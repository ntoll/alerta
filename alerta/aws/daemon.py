import os
import urllib2    # TODO(nsatterl): use Requests instead?
import datetime
import logging
import uuid

import boto.ec2

from alerta.common import config
from alerta.common import log as logging
from alerta.common.daemon import Daemon
from alerta.alert import Alert, Heartbeat
from alerta.alert import syslog
from alerta.common.mq import Messaging

Version = '2.0.0'

LOG = logging.getLogger(__name__)
CONF = config.CONF

BASE_URL = 'http://localhost/alerta/app/v1'

DEFAULT_TIMEOUT = 86400
WAIT_SECONDS = 60

GLOBAL_CONF = '/opt/alerta/alerta/alerta-global.yaml'

LOGFILE = '/var/log/alerta/alert-aws.log'
PIDFILE = '/var/run/alerta/alert-aws.pid'
DISABLE = '/opt/alerta/alerta/alert-aws.disable'
AWSCONF = '/opt/alerta/alerta/alert-aws.yaml'


class AwsDaemon(Daemon):

    info = dict()
    last = dict()
    lookup = dict()

    def run(self):

    #def ec2_status():

        global conn, globalconf, awsconf, BASE_URL, info, last

        if 'endpoint' in globalconf:
            BASE_URL = '%s/alerta/app/v1' % globalconf['endpoint']
        url = '%s/alerts?%s' % (BASE_URL, awsconf.get('filter', 'tags=cloud:AWS/EC2'))

        if 'proxy' in globalconf:
            os.environ['http_proxy'] = globalconf['proxy']['http']
            os.environ['https_proxy'] = globalconf['proxy']['https']

        last = info.copy()
        info = dict()

        for account, keys in awsconf['accounts'].iteritems():
            access_key = keys.get('aws_access_key_id', '')
            secret_key = keys.get('aws_secret_access_key', '')
            logging.debug('AWS Account=%s, AwsAccessKey=%s, AwsSecretKey=************************************%s',
                          account, access_key, secret_key[-4:])

            for region in awsconf['regions']:
                try:
                    ec2 = boto.ec2.connect_to_region(region, aws_access_key_id=access_key,
                                                     aws_secret_access_key=secret_key)
                except boto.exception.EC2ResponseError, e:
                    logging.warning('EC2 API call connect_to_region(region=%s) failed: %s', region, e)
                    continue

                logging.info('Get all instances for account %s in %s', account, region)
                try:
                    reservations = ec2.get_all_instances()
                except boto.exception.EC2ResponseError, e:
                    logging.warning('EC2 API call get_all_instances() failed: %s', e)
                    continue

                instances = [i for r in reservations for i in r.instances if i.tags]
                for i in instances:
                    info[i.id] = dict()
                    info[i.id]['state'] = i.state
                    info[i.id]['stage'] = i.tags.get('Stage', 'unknown')
                    info[i.id]['role'] = i.tags.get('Role', 'unknown')
                    info[i.id]['tags'] = ['os:Linux', 'role:%s' % info[i.id]['role'], 'datacentre:%s' % region,
                                          'virtual:xen', 'cloud:AWS/EC2', 'account:%s' % account]
                    info[i.id]['tags'].append('cluster:%s_%s' % (
                    info[i.id]['role'], region)) # FIXME - replace match on cluster with match on role

                    # FIXME - this is a hack until all EC2 instances are keyed off instance id
                    logging.debug('%s -> %s', i.private_dns_name, i.id)
                    lookup[i.private_dns_name.split('.')[0]] = i.id

                logging.info('Get system and instance status for account %s in %s', account, region)
                try:
                    status = ec2.get_all_instance_status()
                except boto.exception.EC2ResponseError, e:
                    logging.warning('EC2 API call get_all_instance_status() failed: %s', e)
                    continue

                results = dict((i.id,
                                s.system_status.status + ':' + s.instance_status.status) for i in instances for s in status if s.id == i.id)
                for i in instances:
                    if i.id in results:
                        info[i.id]['status'] = results[i.id]
                    else:
                        info[i.id]['status'] = u'not-available:not-available'

        # Get list of all alerts from EC2
        logging.info('Get list of EC2 alerts from %s', url)
        try:
            response = json.loads(urllib2.urlopen(url, None, 15).read())['response']
        except urllib2.URLError, e:
            logging.error('Could not get list of alerts from resources located in EC2: %s', e)
            response = None

        if response and 'alerts' in response and 'alertDetails' in response['alerts']:
            logging.info('Retreived %s EC2 alerts', response['total'])
            alertDetails = response['alerts']['alertDetails']

            for alert in alertDetails:
                alertid = alert['id']
                resource = alert['resource']

                # resource might be 'i-01234567:/tmp'
                if ':' in resource:
                    resource = resource.split(':')[0]

                if resource.startswith('ip-'): # FIXME - transform ip-10-x-x-x to i-01234567
                    logging.debug('%s : Transforming resource %s -> %s', alertid, resource,
                                  lookup.get(resource, resource))
                    resource = lookup.get(resource, resource)

                # Delete alerts for instances that are no longer listed by EC2 API
                if resource not in info:
                    logging.info('%s : EC2 instance %s is no longer listed, DELETE associated alert', alertid, resource)
                    data = '{ "_method": "delete" }'
                    # data = '{ "status": "DELETED" }' # XXX - debug only
                elif info[resource]['state'] == 'terminated' and alert['status'] != 'ACK' and alert['event'] not in [
                    'Ec2InstanceState', 'Ec2StatusChecks']:
                    logging.info('%s : EC2 instance %s is terminated, ACK associated alert', alertid, resource)
                    data = '{ "status": "ACK" }'
                else:
                    continue

                # Delete alert or update alert status
                url = '%s/alerts/alert/%s' % (BASE_URL, alertid)
                logging.debug('%s : %s %s', alertid, url, data)
                req = urllib2.Request(url, data)
                try:
                    response = json.loads(urllib2.urlopen(req).read())['response']
                except urllib2.URLError, e:
                    logging.error('%s : API endpoint error: %s', alertid, e)
                    continue

                if response['status'] == 'ok':
                    logging.info('%s : Successfully updated alert', alertid)
                else:
                    logging.warning('%s : Failed to update alert: %s', alertid, response['message'])

        for instance in info:
            for check, event in [('state', 'Ec2InstanceState'),
                                 ('status', 'Ec2StatusChecks')]:
                if instance not in last or check not in last[instance]:
                    last[instance] = dict()
                    last[instance][check] = 'unknown'

                if last[instance][check] != info[instance][check]:

                    # Defaults
                    resource = instance
                    group = 'AWS/EC2'
                    value = info[instance][check]
                    text = 'Instance was %s now it is %s' % (last[instance][check], info[instance][check])
                    environment = [info[instance]['stage']]
                    service = ['EC2'] # NOTE: Will be transformed to correct service using Ec2ServiceLookup
                    tags = info[instance]['tags']
                    correlate = ''

                    # instance-state = pending | running | shutting-down | terminated | stopping | stopped
                    if check == 'state':
                        if info[instance][check] == 'running':
                            severity = 'NORMAL'
                        else:
                            severity = 'WARNING'

                    # system-status = ok | impaired | initializing | insufficient-data | not-applicable
                    # instance status = ok | impaired | initializing | insufficient-data | not-applicable
                    elif check == 'status':
                        if info[instance][check] == 'ok:ok':
                            severity = 'NORMAL'
                            text = "System and instance status checks are ok"
                        elif info[instance][check].startswith('ok'):
                            severity = 'WARNING'
                            text = 'Instance status check is %s' % info[instance][check].split(':')[1]
                        elif info[instance][check].endswith('ok'):
                            severity = 'WARNING'
                            text = 'System status check is %s' % info[instance][check].split(':')[0]
                        else:
                            severity = 'WARNING'
                            text = 'System status check is %s and instance status check is %s' % tuple(
                                info[instance][check].split(':'))

                    alertid = str(uuid.uuid4()) # random UUID
                    createTime = datetime.datetime.utcnow()

                    headers = dict()
                    headers['type'] = "statusAlert"
                    headers['correlation-id'] = alertid

                    alert = dict()
                    alert['id'] = alertid
                    alert['resource'] = resource
                    alert['event'] = event
                    alert['group'] = group
                    alert['value'] = value
                    alert['severity'] = severity.upper()
                    alert['severityCode'] = SEVERITY_CODE[alert['severity']]
                    alert['environment'] = environment
                    alert['service'] = service
                    alert['text'] = text
                    alert['type'] = 'statusAlert'
                    alert['tags'] = tags
                    alert['summary'] = '%s - %s %s is %s on %s %s' % (
                    ','.join(environment), severity.upper(), event, value, ','.join(service), resource)
                    alert['createTime'] = createTime.replace(microsecond=0).isoformat() + ".%03dZ" % (
                    createTime.microsecond // 1000)
                    alert['origin'] = "%s/%s" % (__program__, os.uname()[1])
                    alert['thresholdInfo'] = 'n/a'
                    alert['timeout'] = DEFAULT_TIMEOUT
                    alert['correlatedEvents'] = correlate

                    self.mq.send(awsAlert)