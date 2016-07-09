from __future__ import print_function

import logging
import os
import names
import time
import socket
import subprocess
import traceback
import ssl
import rosgraph.masterapi
import shutil
import httplib
import sys
import base64
import rosgraph_helper
import key_helper
#from rospy.exceptions import TransportInitError

class GraphModes:
    audit = 'audit'
    complain = 'complain'
    enforce = 'enforce'
    train = 'train'

    class __metaclass__(type):
        def __contains__(self, item):
            return hasattr(self, item)

try:
    import urllib.parse as urlparse #Python 3.x
except ImportError:
    import urlparse

try:
    import xmlrpc.client as xmlrpcclient #Python 3.x
except ImportError:
    import xmlrpclib as xmlrpcclient #Python 2.x

_logger = logging.getLogger('rosgraph.security')

#########################################################################

class Security(object):
    # TODO: add security logging stuff here
    def __init__(self):
        _logger.info("security init")
    def wrap_socket(self, sock, node_name):
        """
        Called whenever there is an opportunity to wrap a server socket.
        The default implementation doesn't do anything.
        """
        return sock
    def xmlrpc_protocol(self):
        return 'http'
    def allow_registerPublisher(self, caller_id, topic, topic_type):
        print('Security.allow_registerPublisher()')
        return True
    def allow_registerSubscriber(self, caller_id, topic, topic_type):
        print('Security.allow_registerSubscriber()')
        return True
    def allow_xmlrpc_request(self, cert_text, cert_binary):
        return True
    def allowClients(self, caller_id, clients):
        return 1, "", 0

#########################################################################

class NoSecurity(Security):

    def __init__(self):
        super(NoSecurity, self).__init__()
        _logger.info("  rospy.security.NoSecurity init")

    def xmlrpcapi(self, uri, node_name = None):
        uriValidate = urlparse.urlparse(uri)
        if not uriValidate[0] or not uriValidate[1]:
            return None
        return xmlrpcclient.ServerProxy(uri)

    def connect(self, sock, dest_addr, dest_port, endpoint_id, timeout=None):
        try:
            _logger.info('connecting to '+str(dest_addr)+' '+str(dest_port))
            sock.connect((dest_addr, dest_port))
        #except TransportInitError as tie:
        #    rospyerr("Unable to initiate TCP/IP socket to %s:%s (%s): %s"%(dest_addr, dest_port, endpoint_id, traceback.format_exc()))
        #    raise
        except Exception as e:
            _logger.warn("Unknown error initiating TCP/IP socket to %s:%s (%s): %s"%(dest_addr, dest_port, endpoint_id, traceback.format_exc()))
            # TODO: figure out how to bubble up and trigger the full close/release behavior in TCPROSTransport
            sock.close() # no reconnection as error is unknown
            raise TransportInitError(str(e)) #re-raise i/o error
        return sock

    def accept(self, server_sock, server_node_name):
        s = server_sock.accept()
        return (s[0], s[1], 'unknown')

#########################################################################
class XMLRPCTimeoutSafeTransport(xmlrpcclient.SafeTransport):
    def __init__(self, context=None, timeout=2.0):
        xmlrpcclient.SafeTransport.__init__(self, context=context)
        self.timeout = timeout
    def make_connection(self, host):
        chost, self._extra_headers, x509 = self.get_host_info(host)
        self._connection = host, httplib.HTTPSConnection(chost, None, timeout=self.timeout, context=self.context, **(x509 or {}))
        return self._connection[1]

#########################################################################
# global functions used by SSLSecurity and friends
def node_name_to_cert_stem(node_name):
    node_name = caller_id_to_node_name(node_name)
    stem = node_name
    if (stem[0] == '/'):
        stem = stem[1:]
    if '..' in node_name:
        raise ValueError('woah there. node_name [%s] has a double-dot. OH NOES.' % node_name)
    return stem.replace('/', '__')

def caller_id_to_node_name(caller_id):
    stem = caller_id
    # check for anonymous node names. remove any long numeric suffix.
    # anonymous nodes will have numeric suffix tokens at the end (PID and time)
    tok = stem.split('_')
    if len(tok) >= 3: # check if pid and epoch suffix posable
        if tok[-2].isdigit() and tok[-1].isdigit(): # check for pid and epoch
            if len(tok[-1]) == 13: # check for epoch 13 uses milliseconds
                stem = '_'.join(tok[0:-2])
    return stem

def open_private_output_file(fn):
    flags = os.O_WRONLY | os.O_CREAT
    return os.fdopen(os.open(fn, flags, 0o600), 'w')

def cert_cn(cert):
    """
    extract the commonName from this certificate
    """
    if cert is None:
        return None
    if not 'subject' in cert:
        return None
    for subject in cert['subject']:
        key = subject[0][0]
        if key == 'commonName':
            return subject[0][1]
    return None

##########################################################################

class SSLSecurity(Security):
    def __init__(self, node_name):
        self.node_name = node_name_to_cert_stem(node_name)
        super(SSLSecurity, self).__init__()
        _logger.info("rospy.security.SSLSecurity init")

        self.kpath = os.path.join(os.environ['ROS_KEYSTORE_PATH'], 'nodes', self.node_name)

        self.master_server_cert_path = self.keystore_path('master', '.server.cert')
        self.server_context_ = None
        self.client_contexts_ = {}
        self.allowed_clients = set()
        self.keyserver_addr_ = None

        if node_name == 'master':
            # some extra initialization steps for graph saving/loading
            self.graphs_path = os.path.join(os.path.expanduser('~'), '.ros', 'graphs')
            if not os.path.exists(self.graphs_path):
                print("initializing empty graph store: %s" % self.graphs_path)
                os.makedirs(self.graphs_path)
                os.chmod(self.graphs_path, 0o700)

            # ugly... need to figure out a graceful way to pass the graph
            self.graph_mode = GraphModes.enforce
            self.graph = rosgraph_helper.GraphStructure()
            if 'ROS_GRAPH_NAME' in os.environ:
                if '..' in os.environ['ROS_GRAPH_NAME'] or '/' in os.environ['ROS_GRAPH_NAME']:
                    raise ValueError("graph name must be a simple string. saw [%s] instead" % os.environ['ROS_GRAPH_NAME'])
                self.graph.graph_path = os.path.join(os.path.expanduser('~'), '.ros', 'graphs', os.environ['ROS_GRAPH_NAME'] + '.yaml')
                if os.path.exists(self.graph.graph_path):
                    self.graph.load_graph()
                if 'ROS_GRAPH_MODE' in os.environ:
                    graph_mode = os.environ['ROS_GRAPH_MODE']
                    self.graph_mode = getattr(GraphModes, graph_mode, GraphModes.enforce)
            else:
                self.graph_mode = None

        if not self.all_certs_present():
            if not os.path.exists(self.kpath):
                print("initializing empty node keystore: %s" % self.kpath)
                os.makedirs(self.kpath)
                os.chmod(self.kpath, 0o700)
            print("calling keyserver_proxy.getCertificates() on %s" % self.ssl_keyserver_addr())
            keyserver_proxy = xmlrpcclient.ServerProxy(self.ssl_keyserver_addr())
            response = keyserver_proxy.getCertificates(self.node_name)
            for fn, contents in response.iteritems():
                path = os.path.join(self.kpath, fn)
                print("  writing [%s]" % path)
                if os.path.isfile(path):
                    continue
                with open_private_output_file(path) as f:
                    f.write(contents)
        else:
            print('all startup certificates are already present')

    def ssl_keyserver_addr(self):
        if self.keyserver_addr_ is not None:
            return self.keyserver_addr_
        if not 'ROS_MASTER_URI' in os.environ:
            self.keyserver_addr_ = 'http://localhost:11310/'
        else:
            # split up the ROS_MASTER_URI and subtract one from its port
            rmu = os.environ['ROS_MASTER_URI']
            host = ':'.join(rmu.split(':')[0:-1])
            port = rmu.split(':')[-1]
            if port[-1] == '/':
                port = port[:-1]
            port = int(port)
            self.keyserver_addr_ = '%s:%d' % (host, port-1)
        print('ssl_keyserver_addr=%s' % self.keyserver_addr_)
        return self.keyserver_addr_

    def all_certs_present(self):
        if not os.path.exists(self.kpath):
            return False
        fn = [ 'root.cert', 'master.server.cert', 'master.client.cert',
               self.node_name + '.server.cert',
               self.node_name + '.server.key',
               self.node_name + '.client.cert',
               self.node_name + '.client.key' ]
        for f in fn:
            if not os.path.isfile(os.path.join(self.kpath, f)):
                return False
        return True

    def keystore_path(self, cert_name, suffix='.cert'):
        return os.path.join(self.kpath, cert_name + suffix)

    def get_server_context(self):
        #print("Security.get_server_context() for node %s" % self.node_name)
        if self.server_context_ is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cafile=self.keystore_path('root','.cert'))
            stem = node_name_to_cert_stem(self.node_name)
            keyfile  = os.path.join(self.kpath, stem + '.server.key')
            certfile = os.path.join(self.kpath, stem + '.server.cert')
            if not os.path.isfile(keyfile) or not os.path.isfile(certfile):
                raise Exception("ahhhhhhHHHHHHHHHHHH where did all the certs go?")
            context.load_cert_chain(certfile, keyfile=keyfile)
            self.server_context_ = context
        return self.server_context_

    def get_client_context(self, server):
        #print("Security.get_client_context(%s) for node %s" % (server, self.node_name))
        if not server in self.client_contexts_:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_REQUIRED
            stem = node_name_to_cert_stem(self.node_name)
            context.load_verify_locations(cafile=self.keystore_path('root', '.cert'))
            keyfile  = os.path.join(self.kpath, stem + '.client.key')
            certfile = os.path.join(self.kpath, stem + '.client.cert')
            context.load_cert_chain(certfile, keyfile=keyfile)
            self.client_contexts_[server] = context
        return self.client_contexts_[server]

    def wrap_socket(self, sock, node_name):
        """
        Called whenever there is an opportunity to wrap a server socket
        """
        #print("SSLSecurity.wrap_socket() called for node %s" % node_name)
        context = self.get_server_context()
        return context.wrap_socket(sock, server_side=True) #connstream

    def xmlrpcapi(self, uri, node_name=None):
        #print("SSLSecurity.xmlrpcapi(%s, %s)" % (uri, node_name or "[UNKNOWN]"))
        context = self.get_client_context(node_name or uri)
        st = XMLRPCTimeoutSafeTransport(context=context, timeout=10.0)
        return xmlrpcclient.ServerProxy(uri, transport=st, context=context)

    def xmlrpc_protocol(self):
        return 'https'

    def connect(self, sock, dest_addr, dest_port, endpoint_id, timeout=None):
        #print('SSLSecurity.connect() to node at %s:%d which is endpoint %s' % (dest_addr, dest_port, endpoint_id))
        context = self.get_client_context("%s:%d" % (dest_addr, dest_port))
        conn = context.wrap_socket(sock)
        try:
            conn.connect((dest_addr, dest_port))
        except Exception as e:
            print("  AHHH node %s SSLSecurity.connect() exception: %s" % (self.node_name, e))
            raise
        return conn

    def accept(self, server_sock, server_node_name):
        # this is where peer-to-peer TCPROS connections try to be accepted
        #print("SSLSecurity.accept(server_node_name=%s)" % server_node_name)
        context = self.get_server_context()
        (client_sock, client_addr) = server_sock.accept()
        client_stream = None
        try:
            client_stream = context.wrap_socket(client_sock, server_side=True)
            client_cert = client_stream.getpeercert()
            #print("SSLSecurity.accept(server_node_name=%s) getpeercert = %s" % (server_node_name, repr(client_cert)))
            cn = cert_cn(client_cert)
            if cn is None:
                raise Exception("unknown certificate format")
            if not cn.endswith('.client'):
                raise Exception("unexpected commonName format: %s" % cn)
            # get rid of the '.client' suffix
            cn = '.'.join(cn.split('.')[0:-1])
            if not cn in self.allowed_clients:
                raise Exception("unknown client [%s] is trying to connect!" % cn)
        except Exception as e:
            print("SSLSecurity.accept() wrap_socket exception: %s" % e)
            raise
        print("%s is allowing inbound connection from %s" % (self.node_name, cn))
        return (client_stream, client_addr, cn)


    # only ever called by master
    def allowClients(self, caller_id, clients):
        #print("node=%s allowClients(%s)" % (self.node_name, repr(clients)))
        for client in clients:
            sanitized_name = node_name_to_cert_stem(client)
            #print("  allowed_clients.add(%s)" % sanitized_name)
            self.allowed_clients.add(sanitized_name)
        # todo: save certificate in our keystore?
        # todo: allow only for specific topics, not always all-access?
        return 1, "", 0

    def allow_xmlrpc_request(self, cert_text, cert_binary):
        cn = cert_cn(cert_text)
        if cn is None:
            return None
        #print("allow_xmlrpc_request() node=%s commonName=%s" % (self.node_name, cn))
        # sanity-check to ensure that it ends in '.client'
        if not cn.endswith('.client'):
            return None
        try:
            # if we're master, make sure this cert exists in our keystore
            if self.node_name == 'master': # NOW I AM THE MASTER
                path = os.path.join(self.kpath, cn + '.cert')
                if '..' in path:
                    print('HEY WHAT ARE YOU TRYING TO DO WITH THOSE DOTS')
                    return None
                #print('looking for client cert in [%s]' % path)
                if not os.path.isfile(path):
                    print("WOAH THERE PARTNER. I DON'T KNOW [%s]" % cn)
                    return None
                with open(path, 'r') as client_cert_file:
                    file_contents = client_cert_file.read()
                    # remove header and footer lines
                    file_contents = ''.join(file_contents.split('\n')[1:-2])
                #print('file contents: %s' % file_contents)
                client_cert = base64.b64encode(cert_binary)
                #print('cert transmitted: %s' % client_cert)
                if file_contents != client_cert:
                    print('WOAH. cert mismatch!')
                    return None
                else:
                    #print('    cert matches OK')
                    return cn[:-7]
            else: # we're not the master. life is more complicated.
                # see if it's the master
                if cn == 'master.client':
                    # we have this one in our keystore; we can verify it
                    cert_path = os.path.join(self.kpath, 'master.client.cert')
                    with open(cert_path, 'r') as cert_file:
                        saved_cert = cert_file.read()
                        # remove header and footer lines
                        saved_cert = ''.join(saved_cert.split('\n')[1:-2])
                    presented_cert = base64.b64encode(cert_binary)
                    if saved_cert != presented_cert:
                        print("WOAH. master client certificate mismatch!")
                        return None
                    else:
                        #print("   master client cert matches OK")
                        return 'master'
                # see if we are talking to ourselves
                cn_without_suffix = '.'.join(cn.split('.')[0:-1])
                if cn_without_suffix == self.node_name:
                    #print("    I'm talking to myself again.")
                    return cn_without_suffix # it's always healthy to talk to yourself
                if cn_without_suffix in self.allowed_clients:
                    #print("    client %s is OK; it's in %s's allowed_clients list: %s" % (cn_without_suffix, self.node_name, repr(self.allowed_clients)))
                    return cn_without_suffix
                else:
                    print("    client %s xmlrpc connection denied; it's not in %s's allowed list of clients: %s" % (cn_without_suffix, self.node_name, repr(self.allowed_clients)))
                    return None

        except Exception as e:
            print('oh noes: %s' % e)
            return None
        print("WOAH WOAH WOAH how did i get here?")
        return 'ahhhhhhhhhhhh'

    def log_register(self, enable, flag, info):
        if enable:
            _logger.info("{}REG [{}]:{} {} to graph:[{}]".format(flag, *info))

    def allow_register(self, caller_id, topic_name, topic_type, mask):
        node_name = caller_id_to_node_name(caller_id)
        info = (topic_name, mask, node_name, self.graph.graph_path)
        if self.graph_mode is GraphModes.enforce:
            allowed, audit = self.graph.is_allowed(node_name, topic_name, mask)
            flag = '*' if allowed else '!'
            self.log_register(audit, flag, info)
            return allowed
        elif self.graph_mode is GraphModes.train:
            if self.graph.graph_path is not None:
                allowed, audit = self.graph.is_allowed(node_name, topic_name, mask)
                if not allowed:
                    self.graph.add_allowed(node_name, topic_name, mask)
                    self.graph.save_graph()
                flag = '*' if allowed else '+'
                self.log_register((audit or not allowed), flag, info)
            return True
        elif self.graph_mode is GraphModes.complain:
            if self.graph.graph is not None:
                allowed, audit = self.graph.is_allowed(node_name, topic_name, mask)
                flag = '*' if allowed else '!'
                self.log_register((audit or not allowed), flag, info)
            else:
                self.log_register(True, '!', info)
            return True
        elif self.graph_mode is GraphModes.audit:
            if self.graph.graph is not None:
                allowed, audit = self.graph.is_allowed(node_name, topic_name, mask)
                flag = '*' if allowed else '!'
                self.log_register(True, flag, info)
                return allowed
            else:
                self.log_register(True, '', info)
                return True
        else:
            return False

    def allow_registerPublisher(self, caller_id, topic, topic_type):
        if self.graph_mode is None:
            return True
        else:
            return self.allow_register(caller_id, topic, topic_type, 'w')

    def allow_registerSubscriber(self, caller_id, topic, topic_type):
        if self.graph_mode is None:
            return True
        else:
            return self.allow_register(caller_id, topic, topic_type, 'r')

#########################################################################
_security = None
def init(node_name):
    #print('security.init(%s)' % node_name)
    global _security
    if _security is None:
        _logger.info("choosing security model...")
        if 'ROS_SECURITY' in os.environ:
            if os.environ['ROS_SECURITY'] == 'ssl' or os.environ['ROS_SECURITY'] == 'ssl_setup':
                _security = SSLSecurity(node_name)
            else:
                raise ValueError("illegal ROS_SECURITY value: [%s]" % os.environ['ROS_SECURITY'])
        else:
            _security = NoSecurity()

def get():
    if _security is None:
        import traceback
        traceback.print_stack()
        raise ValueError("woah there partner. security.init() wasn't called before security.get()")
    return _security
