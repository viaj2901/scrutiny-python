import copy
import queue
import time
import logging
import binascii
from enum import Enum
import traceback

from scrutiny.server.protocol.comm_handler import CommHandler
from scrutiny.server.protocol import Protocol, ResponseCode
from scrutiny.server.device.request_dispatcher import RequestDispatcher
from scrutiny.server.device.request_generator.device_searcher import DeviceSearcher
from scrutiny.server.device.request_generator.heartbeat_generator import HeartbeatGenerator
from scrutiny.server.device.request_generator.info_poller import InfoPoller
from scrutiny.server.device.request_generator.session_initializer import SessionInitializer
from scrutiny.core.firmware_id import PLACEHOLDER as DEFAULT_FIRMWARE_ID
from scrutiny.server.server_tools import Timer

DEFAULT_FIRMWARE_ID_ASCII = binascii.hexlify(DEFAULT_FIRMWARE_ID).decode('ascii')

class DeviceHandler:
    DEFAULT_PARAMS = {
            'response_timeout' : 1.0,    # If a response take more than this delay to be received after a request is sent, drop the response.
            'heartbeat_timeout' : 4.0,
            'default_address_size' : 32,
            'default_protocol_version' : '1.0'
        }

    class RequestPriority:
        Disconnect = 0  
        Heatbeat = 1
        Connect = 2
        PollInfo = 5
        Discover = 10


    class ConnectionStatus(Enum):
        UNKNOWN = -1
        DISCONNECTED = 0
        CONNECTING = 1
        CONNECTED_NOT_READY = 2
        CONNECTED_READY = 3

    class FsmState(Enum):
        INIT = 0
        DISCOVERING = 1
        CONNECTING = 2
        POLLING_INFO = 3
        READY = 4
        DISCONNECTING = 5 

    def __init__(self, config, datastore):
        self.logger = logging.getLogger(self.__class__.__name__)

        self.config = copy.copy(self.DEFAULT_PARAMS)
        self.config.update(config)
        self.datastore = datastore
        self.dispatcher = RequestDispatcher()
        (major, minor) = self.config['default_protocol_version'].split('.')
        self.protocol = Protocol(major, minor)
        self.device_searcher = DeviceSearcher(self.protocol, self.dispatcher, priority=self.RequestPriority.Discover)
        self.session_initializer = SessionInitializer(self.protocol, self.dispatcher, priority=self.RequestPriority.Connect)
        self.heartbeat_generator = HeartbeatGenerator(self.protocol, self.dispatcher, priority=self.RequestPriority.Heatbeat)
        self.info_poller = InfoPoller(
            self.protocol, 
            self.dispatcher, 
            priority=self.RequestPriority.PollInfo, 
            protocol_version_callback = self.get_protocol_version_callback, # Called when protocol version is polled
            comm_param_callback = self.get_comm_params_callback,            # Called when communication params are polled
            )

        self.comm_handler = CommHandler(self.config)

        self.heartbeat_generator.set_interval(max(0.5, self.config['heartbeat_timeout'] * 0.75))
        self.comm_broken = False
        self.device_id = None

        self.reset_comm()

    def get_device_info(self):
        return copy.copy(self.device_info)

    def get_comm_error_count(self):
        return self.comm_broken_count

    def get_comm_params_callback(self, partial_device_info):
        # In the POLLING_INFO stage, there is a point where we will have gotten the communication params.
        # This callback is called right after it so we can adapt.
        # We can raise exception here. 
        # They will be logged by info_poller. info_poller will go to error state. DeviceHandler will notice that and reset communication

        if not isinstance(partial_device_info.address_size_bits, int) :
            raise Exception('Address size gotten from device not valid.')

        if partial_device_info.address_size_bits not in [8,16,32,64]:
            raise Exception("The device have an address size of %d bits. This server only supports 8,16,32,64 bits" % (partial_device_info.address_size_bits)) 

        if not isinstance( partial_device_info.heartbeat_timeout_us, int):
            raise Exception('Heartbeat timeout gotten from device is invalid')

        self.logger.info('Device has an address size of %d bits. Configuring protocol to encode/decode them accordingly.' % partial_device_info.address_size_bits)
        self.protocol.set_address_size(partial_device_info.address_size_bits)

        self.heartbeat_generator.set_interval(max(0.5, float(partial_device_info.heartbeat_timeout_us)/1000000.0 * 0.75))

    def get_protocol_version_callback(self, major, minor):
        # In the POLLING_INFO stage, there is a point where we will have gotten the communication params.
        # This callback is called right after it so we can adapt.
        # We can raise exception here. 
        # They will be logged by info_poller. info_poller will go to error state. DeviceHandler will notice that and reset communication

        if not isinstance(major, int) or not isinstance(minor, int):
            raise Exception('Protocol version gotten from device not valid.')

        self.logger.info('Configuring protocol to V%d.%d' % (major, minor))
        self.protocol.set_version(major, minor)   # This may raise an exception
        

    # Tells the state of our connection with the device.
    def get_connection_status(self):
        if self.connected:
            if self.fsm_state == self.FsmState.READY:
                return self.ConnectionStatus.CONNECTED_READY
            else:
                return self.ConnectionStatus.CONNECTED_NOT_READY

        if self.comm_broken:
            return self.ConnectionStatus.DISCONNECTED

        if self.fsm_state == self.FsmState.CONNECTING:
            return self.ConnectionStatus.CONNECTING

        if not self.connected:
            return self.ConnectionStatus.DISCONNECTED

        return self.ConnectionStatus.UNKNOWN


    def get_comm_link(self):
        return self.comm_handler.get_link()

    # Set communication state to a fresh start.
    def reset_comm(self):
        if self.comm_broken and self.device_id is not None:
            self.logger.info('Communication with device stopped. Searching for a new device')

        self.connected = False
        self.fsm_state = self.FsmState.INIT
        self.last_fsm_state = self.FsmState.INIT
        self.active_request_record = None
        self.device_id = None
        self.comm_broken = False
        self.device_searcher.stop()
        self.heartbeat_generator.stop()
        self.info_poller.stop()
        self.session_initializer.stop()
        self.session_id = None
        self.disconnection_requested = False
        self.disconnect_callback = None
        self.disconnect_complete = False
        self.comm_broken_count = 0
        self.protocol.set_address_size(self.config['default_address_size']) # Set back the protocol to decode addresses of this size.
        (major, minor) = self.config['default_protocol_version'].split('.')
        self.protocol.set_version(int(major), int(minor))

    # Open communication channel based on config
    def init_comm(self):
        if self.config['link_type'] == 'none':
            return

        if self.config['link_type'] == 'udp':
            from .links.udp_link import UdpLink
            link_class = UdpLink
        elif self.config['link_type'] == 'dummy':
            from .links.dummy_link import DummyLink
            link_class = DummyLink
        elif self.config['link_type'] == 'thread_safe_dummy':
            from .links.dummy_link import ThreadSafeDummyLink
            link_class = ThreadSafeDummyLink
        else:
            raise ValueError('Unknown link type %s' % self.config['link_type'])

        device_link = link_class(self.config['link_config'])    #instantiate the class
        self.comm_handler.open(device_link)
        self.reset_comm()

    def send_disconnect(self, disconnect_callback=None):
        self.logger.debug('Disconnection requested.')
        self.disconnection_requested = True
        self.disconnect_callback = disconnect_callback

    # Stop all communication with the device
    def stop_comm(self):
        if self.comm_handler is not None:
            self.comm_handler.close()
        self.reset_comm()

    def refresh_vars(self):
        pass

    # To be called periodically
    def process(self):
        self.device_searcher.process()
        self.heartbeat_generator.process()
        self.info_poller.process()
        self.session_initializer.process()

        self.handle_comm()      # Make sure request and response are being exchanged with the device
        self.do_state_machine()
        

    def do_state_machine(self):
        if self.comm_broken:
            self.comm_broken_count += 1
            self.fsm_state = self.FsmState.INIT

        if self.connected:
            time.time() - self.heartbeat_generator.last_valid_heartbeat_timestamp() > self.config['heartbeat_timeout']

        # ===   FSM  ===
        state_entry = True if self.fsm_state != self.last_fsm_state else False
        next_state = self.fsm_state
        if self.fsm_state == self.FsmState.INIT:
            self.reset_comm()
            if self.comm_handler.is_open():
                next_state = self.FsmState.DISCOVERING

        #============= [DISCOVERING] =====================
        elif self.fsm_state == self.FsmState.DISCOVERING:
            if state_entry:
                self.device_searcher.start()

            found_device_id = self.device_searcher.get_found_device_ascii()
            if found_device_id is not None:
                if self.device_id is None:
                    self.logger.info('Found a device - %s' % found_device_id)
                    self.device_id = found_device_id

                    if found_device_id == DEFAULT_FIRMWARE_ID_ASCII:
                        self.logger.warning("Firmware ID of this device is a default placeholder. Firmware might not have been tagged with a valid ID in the build toolchain.")

            if self.device_id is not None:
                self.device_searcher.stop()
                next_state = self.FsmState.CONNECTING

        #============= [CONNECTING] =====================
        elif self.fsm_state == self.FsmState.CONNECTING:
            # Connection message can be handled synchronously as no request generator is active.
            # In other conditions, we should use the dispatcher and do everything asynchronously.
            if state_entry:
                self.session_initializer.start()


            if self.session_initializer.connection_successful():
                self.session_initializer.stop()
                self.session_id = self.session_initializer.get_session_id()
                self.logger.debug("Session ID set : 0x%08x" % self.session_id)
                self.heartbeat_generator.set_session_id(self.session_id)
                self.heartbeat_generator.start()    # This guy will send recurrent heartbeat request. If that request fails (timeout), comm will be reset
                self.connected = True
                self.logger.info('Connected to device %s with session ID 0x%08X' % (self.device_id, self.session_id))
                next_state = self.FsmState.POLLING_INFO
            elif self.session_initializer.is_in_error():
                self.session_initializer.stop()
                self.comm_broken = True
            elif self.disconnection_requested:
                next_state = self.FsmState.DISCONNECTING

        # ========= [POLLING_INFO] =======
        elif self.fsm_state == self.FsmState.POLLING_INFO:
            if self.disconnection_requested:
                next_state = self.FsmState.DISCONNECTING

            if state_entry:
                self.info_poller.start()

            if self.info_poller.is_in_error():
                self.logger.info('Impossible to poll data from the device. Restarting communication')
                next_state = self.FsmState.INIT

            elif self.info_poller.done():
                self.device_info = self.info_poller.get_device_info()   # Make a copy if the data fetched by the infoPoller
                self.info_poller.stop()

                if not self.device_info.all_ready():    # No property should be None
                    self.logger.error('Data polled from device is incomplete. Restarting communication. %s')
                    self.logger.debug(str(self.device_info))
                    next_state = self.FsmState.INIT
                else:
                    next_state = self.FsmState.READY

        # ========= [READY] ==========
        elif self.fsm_state == self.FsmState.READY:
            if state_entry:
                self.logger.info('Communication with device %s fully ready' % (self.device_id))
                self.logger.debug("Device information : %s" % self.device_info)

            if self.disconnection_requested:
                next_state = self.FsmState.DISCONNECTING
       
       # ========= [DISCONNECTING] ==========
        elif self.fsm_state == self.FsmState.DISCONNECTING:
            if state_entry:
                self.disconnect_complete = False
            
            if not self.connected:
                next_state = self.FsmState.INIT
            else:
                if state_entry:
                    self.dispatcher.register_request(
                        request = self.protocol.comm_disconnect(self.session_id),
                        success_callback = self.disconnect_complete_success,
                        failure_callback = self.disconnect_complete_failure,
                        priority=self.RequestPriority.Disconnect
                    )

            if self.disconnect_complete:
                next_state != self.FsmState.DISCONNECTING                

        else:
            raise Exception('Unknown FSM state : %s' % self.fsm_state)


        # ====  FSM END ====

        self.last_fsm_state = self.fsm_state
        if next_state != self.fsm_state:
            self.logger.debug('Moving FSM to state %s' % next_state)
        self.fsm_state = next_state

    def disconnect_complete_success(self, request, response_code, response_data, params=None):
        self.disconnect_complete = True
        if self.disconnect_callback is not None:
            self.disconnect_callback.__call__(True)

    def disconnect_complete_failure(request, params=None):
        self.disconnect_complete = True
        if self.disconnect_callback is not None:
            self.disconnect_callback.__call__(False)


    def handle_comm(self):
        self.comm_handler.process()     # Process reception

        if not self.comm_handler.is_open():
            return
        
        if self.active_request_record is None:  # We haven't send a request
            record = self.dispatcher.next()
            if record is not None:              # A new request to send
                self.active_request_record = record
                self.comm_handler.send_request(record.request)
        else:
            if self.comm_handler.has_timed_out():       # The request we have sent has timed out.. no response
                self.logger.debug('Request timed out. %s' % self.active_request_record.request)
                self.comm_broken = True
                self.comm_handler.clear_timeout()
                self.active_request_record.complete(success=False)

            elif self.comm_handler.waiting_response():      # We are still wiating for a resonse
                if self.comm_handler.response_available():  # We got a response! yay
                    response = self.comm_handler.get_response()

                    try:
                        data = self.protocol.parse_response(response)
                        self.active_request_record.complete(success=True, response=response, response_data=data) # Valid response if we get here.
                    except Exception as e:                   # Malformed response.
                        self.comm_broken = True
                        self.logger.error("Invalid response received. %s" % str(e))
                        self.logger.debug(traceback.format_exc())
                        self.active_request_record.complete(success=False)
            
            else:   # Comm handler decided to go back to Idle by itself. Most likely a valid message that was not the response of the request.
                self.comm_broken = True
                self.comm_handler.reset() 
                self.active_request_record.complete(success=False)

            if self.active_request_record is not None:          # double check if None here in case the user shut down communication in a callback
                if self.active_request_record.is_completed():   # If we have called a callback, then we are done with this request.
                    self.active_request_record = None

        self.comm_handler.process()      # Process new transmission now.

