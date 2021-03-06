import os
import sys
import time
import threading
import urllib2
import json
import smtplib
# from email.MIMEMultipart import MIMEMultipart
# from email.MIMEText import MIMEText
# from email.MIMEImage import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import stomp
import datetime
import pytz
import uuid

from alerta.common import config
from alerta.common import log as logging
from alerta.common.daemon import Daemon
from alerta.alert import Alert, Heartbeat
from alerta.common.mq import Messaging

Version = '2.0.0'

LOG = logging.getLogger(__name__)
CONF = config.CONF

BROKER_LIST = [('localhost', 61613)] # list of brokers for failover
NOTIFY_TOPIC = '/topic/notify'
ALERTA_URL = 'http://monitoring.guprod.gnl'
SMTP_SERVER = 'mx'
ALERTER_MAIL = 'alerta@guardian.co.uk'
# MAILING_LIST = ['nick.satterly@guardian.co.uk', 'simon.huggins@guardian.co.uk']
MAILING_LIST = ['nick.satterly@guardian.co.uk']
TIMEZONE = 'Europe/London'

DISABLE = '/opt/alerta/alerta/alert-mailer.disable'
LOGFILE = '/var/log/alerta/alert-mailer.log'
PIDFILE = '/var/run/alerta/alert-mailer.pid'

_TokenThread = None            # Worker thread object
_Lock = threading.Lock()       # Synchronization lock
TOKEN_LIMIT = 20
_token_rate = 30               # Add a token every 30 seconds
tokens = 20


class MailerDaemon(Daemon):

    global conn

    LOG.basicConfig(level=LOG.INFO, format="%(asctime)s alert-mailer[%(process)d] %(levelname)s - %(message)s",
                        filename=LOGFILE)
    LOG.info('Starting up Alert Mailer version %s', __version__)

    # Write pid file if not already running
    if os.path.isfile(PIDFILE):
        pid = open(PIDFILE).read()
        try:
            os.kill(int(pid), 0)
            LOG.error('Process with pid %s already exists, exiting', pid)
            sys.exit(1)
        except OSError:
            pass
    file(PIDFILE, 'w').write(str(os.getpid()))

    while os.path.isfile(DISABLE):
        LOG.warning('Disable flag exists (%s). Sleeping...', DISABLE)
        time.sleep(120)

    # Connect to message broker
    try:
        conn = stomp.Connection(
            BROKER_LIST,
            reconnect_sleep_increase=5.0,
            reconnect_sleep_max=120.0,
            reconnect_attempts_max=20
        )
        conn.set_listener('', MessageHandler())
        conn.start()
        conn.connect(wait=True)
        conn.subscribe(destination=NOTIFY_TOPIC)
    except Exception, e:
        LOG.error('Stomp connection error: %s', e)

    # Start token bucket thread
    LOG.info('Start token bucket rate limiting thread')
    _TokenThread = TokenTopUp()
    _TokenThread.start()

    while True:
        try:
            time.sleep(0.01)
        except (KeyboardInterrupt, SystemExit):
            conn.disconnect()
            _TokenThread.shutdown()
            os.unlink(PIDFILE)
            sys.exit(0)


class MessageHandler(object):
    def on_error(self, headers, body):
        LOG.error('Received an error %s', body)

    def on_message(self, headers, body):
        global tokens

        LOG.debug("Received alert : %s", body)

        alert = dict()
        alert = json.loads(body)

        LOG.info('%s : [%s] %s', alert['lastReceiveId'], alert['status'], alert['summary'])

        # Only send a NORMAL email for alerts that have cleared
        if alert['severity'] == 'NORMAL' and alert['previousSeverity'] == 'UNKNOWN':
            LOG.info('%s : Skip this NORMAL alert because it is not clearing a known alarm', alert['lastReceiveId'])
            return

        # WARNINGs to/from NORMAL or UNKNOWN severity should not trigger emails
        if ((alert['severity'] == 'WARNING' and alert['previousSeverity'] in ['NORMAL', 'UNKNOWN']) or
                (alert['severity'] == 'NORMAL' and alert['previousSeverity'] == 'WARNING')):
            LOG.info('%s : Skip this state change to/from WARNING alert because warnings should not trigger emails',
                         alert['lastReceiveId'])
            return

        if tokens:
            _Lock.acquire()
            tokens -= 1
            _Lock.release()
            LOG.debug('Taken a token, there are only %d left', tokens)
        else:
            LOG.warning('%s : No tokens left, rate limiting this alert', alert['lastReceiveId'])
            return

        # Convert createTime to local time (set TIMEZONE above)
        createTime = datetime.datetime.strptime(alert['createTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
        createTime = createTime.replace(tzinfo=pytz.utc)
        tz = pytz.timezone(TIMEZONE)
        localTime = createTime.astimezone(tz)

        text = ''
        text += '[%s] %s\n' % (alert['status'], alert['summary'])
        text += 'Alert Details\n'
        text += 'Alert ID: %s\n' % (alert['id'])
        text += 'Create Time: %s\n' % (localTime.strftime('%Y/%m/%d %H:%M:%S'))
        text += 'Resource: %s\n' % (alert['resource'])
        text += 'Environment: %s\n' % (','.join(alert['environment']))
        text += 'Service: %s\n' % (','.join(alert['service']))
        text += 'Event Name: %s\n' % (alert['event'])
        text += 'Event Group: %s\n' % (alert['group'])
        text += 'Event Value: %s\n' % (alert['value'])
        text += 'Severity: %s -> %s\n' % (alert['previousSeverity'], alert['severity'])
        text += 'Status: %s\n' % (alert['status'])
        text += 'Text: %s\n' % (alert['text'])
        if 'thresholdInfo' in alert:
            text += 'Threshold Info: %s\n' % (alert['thresholdInfo'])
        if 'duplicateCount' in alert:
            text += 'Duplicate Count: %s\n' % (alert['duplicateCount'])
        if 'moreInfo' in alert:
            text += 'More Info: %s\n' % (alert['moreInfo'])
        text += 'Historical Data\n'
        if 'graphs' in alert:
            for g in alert['graphs']:
                text += '%s\n' % (g)
        text += 'Raw Alert\n'
        text += '%s\n' % (json.dumps(alert))
        text += 'Generated by %s on %s at %s\n' % (
            'alert-mailer.py', os.uname()[1], datetime.datetime.now().strftime("%a %d %b %H:%M:%S"))

        LOG.debug('Raw Text: %s', text)

        html = '<p><table border="0" cellpadding="0" cellspacing="0" width="100%">\n'  # table used to center email
        html += '<tr><td bgcolor="#ffffff" align="center">\n'
        html += '<table border="0" cellpadding="0" cellspacing="0" width="700">\n'     # table used to set width of email
        html += '<tr><td bgcolor="#425470"><p align="center" style="font-size:24px;color:#d9fffd;font-weight:bold;"><strong>[%s] %s</strong></p>\n' % (
            alert['status'], alert['summary'])

        html += '<tr><td><p align="left" style="font-size:18px;line-height:22px;color:#c25130;font-weight:bold;">Alert Details</p>\n'
        html += '<table>\n'
        html += '<tr><td><b>Alert ID:</b></td><td><a href="%s/alerta/details.php?id=%s" target="_blank">%s</a></td></tr>\n' % (
            ALERTA_URL, alert['id'], alert['id'])
        html += '<tr><td><b>Create Time:</b></td><td>%s</td></tr>\n' % (localTime.strftime('%Y/%m/%d %H:%M:%S'))
        html += '<tr><td><b>Resource:</b></td><td>%s</td></tr>\n' % (alert['resource'])
        html += '<tr><td><b>Environment:</b></td><td>%s</td></tr>\n' % (','.join(alert['environment']))
        html += '<tr><td><b>Service:</b></td><td>%s</td></tr>\n' % (','.join(alert['service']))
        html += '<tr><td><b>Event Name:</b></td><td>%s</td></tr>\n' % (alert['event'])
        html += '<tr><td><b>Event Group:</b></td><td>%s</td></tr>\n' % (alert['group'])
        html += '<tr><td><b>Event Value:</b></td><td>%s</td></tr>\n' % (alert['value'])
        html += '<tr><td><b>Severity:</b></td><td>%s -> %s</td></tr>\n' % (alert['previousSeverity'], alert['severity'])
        html += '<tr><td><b>Status:</b></td><td>%s</td></tr>\n' % (alert['status'])
        html += '<tr><td><b>Text:</b></td><td>%s</td></tr>\n' % (alert['text'])
        if 'thresholdInfo' in alert:
            html += '<tr><td><b>Threshold Info:</b></td><td>%s</td></tr>\n' % (alert['thresholdInfo'])
        if 'duplicateCount' in alert:
            html += '<tr><td><b>Duplicate Count:</b></td><td>%s</td></tr>\n' % (alert['duplicateCount'])
        if 'moreInfo' in alert:
            html += '<tr><td><b>More Info:</b></td><td><a href="%s">ganglia</a></td></tr>\n' % (alert['moreInfo'])
        html += '</table>\n'
        html += '</td></tr>\n'
        html += '<tr><td><p align="left" style="font-size:18px;line-height:22px;color:#c25130;font-weight:bold;">Historical Data</p>\n'
        if 'graphs' in alert:
            graph_cid = dict()
            for g in alert['graphs']:
                graph_cid[g] = str(uuid.uuid4())
                html += '<tr><td><img src="cid:' + graph_cid[g] + '"></td></tr>\n'
        html += '<tr><td><p align="left" style="font-size:18px;line-height:22px;color:#c25130;font-weight:bold;">Raw Alert</p>\n'
        html += '<tr><td><p align="left" style="font-family: \'Courier New\', Courier, monospace">%s</p></td></tr>\n' % (
            json.dumps(alert))
        html += '<tr><td>Generated by %s on %s at %s</td></tr>\n' % (
            'alert-mailer.py', os.uname()[1], datetime.datetime.now().strftime("%a %d %b %H:%M:%S"))
        html += '</table>'
        html += '</td></tr></table>'
        html += '</td></tr></table>'

        LOG.debug('HTML Text %s', html)

        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = '[%s] %s' % (alert['status'], alert['summary'])
        msg_root['From'] = ALERTER_MAIL
        msg_root['To'] = ','.join(MAILING_LIST)
        msg_root.preamble = 'This is a multi-part message in MIME format.'

        msg_alt = MIMEMultipart('alternative')
        msg_root.attach(msg_alt)

        msg_text = MIMEText(text, 'plain')
        msg_alt.attach(msg_text)

        msg_html = MIMEText(html, 'html')
        msg_alt.attach(msg_html)

        if 'graphs' in alert:
            msg_img = dict()
            for g in alert['graphs']:
                try:
                    image = urllib2.urlopen(g).read()
                    msg_img[g] = MIMEImage(image)
                    LOG.debug('graph cid %s', graph_cid[g])
                    msg_img[g].add_header('Content-ID', '<' + graph_cid[g] + '>')
                    msg_root.attach(msg_img[g])
                except:
                    pass

        try:
            LOG.info('%s : Send email to %s', alert['lastReceiveId'], ','.join(MAILING_LIST))
            s = smtplib.SMTP(SMTP_SERVER)
            # s.set_debuglevel(1) # XXX - uncomment for detailed SMTP debugging
            s.sendmail(ALERTER_MAIL, MAILING_LIST, msg_root.as_string())
            s.quit()
        except smtplib.SMTPException, e:
            LOG.error('%s : Sendmail failed - %s', alert['lastReceiveId'], e)

    def on_disconnected(self):
        global conn

        LOG.warning('Connection lost. Attempting auto-reconnect to %s', NOTIFY_TOPIC)
        conn.start()
        conn.connect(wait=True)
        conn.subscribe(destination=NOTIFY_TOPIC)


class TokenTopUp(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.running = False
        self.shuttingdown = False

    def shutdown(self):
        self.shuttingdown = True
        if not self.running:
            return
        self.join()

    def run(self):
        global _token_rate, tokens
        self.running = True

        while not self.shuttingdown:
            if self.shuttingdown:
                break

            if tokens < TOKEN_LIMIT:
                _Lock.acquire()
                tokens += 1
                _Lock.release()

            if not self.shuttingdown:
                LOG.debug('Added token to bucket. There are now %d tokens', tokens)
                time.sleep(_token_rate)

        self.running = False

