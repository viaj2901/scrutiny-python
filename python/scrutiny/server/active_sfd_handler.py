#    active_sfd_handler.py
#        Manage the loaded SFD file with which the client will interracts.
#
#   - License : MIT - See LICENSE file.
#   - Project : Scrutiny Debugger (github.com/scrutinydebugger/scrutiny)
#
#   Copyright (c) 2021-2022 scrutinydebugger

import logging
import enum
import traceback

from scrutiny.core import FirmwareDescription
from scrutiny.core.sfd_storage import SFDStorage
from scrutiny.server.device.device_handler import DeviceHandler
from scrutiny.server.datastore import Datastore, DatastoreEntry

from typing import Optional, List
from scrutiny.core.typehints import GenericCallback, Callable


class SFDLoadedCallback(GenericCallback):
    callback: Callable[[FirmwareDescription], None]


class SFDUnloadedCallback(GenericCallback):
    callback: Callable[[None], None]


class ActiveSFDHandler:
    logger: logging.Logger
    device_handler: DeviceHandler
    datastore: Datastore
    autoload: bool

    sfd: Optional[FirmwareDescription]
    previous_device_status: DeviceHandler.ConnectionStatus
    requested_firmware_id: Optional[str]

    loaded_callbacks: List[SFDLoadedCallback]
    unloaded_callbacks: List[SFDUnloadedCallback]

    def __init__(self, device_handler: DeviceHandler, datastore: Datastore, autoload=True):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.device_handler = device_handler
        self.datastore = datastore
        self.autoload = autoload

        self.sfd = None
        self.previous_device_status = DeviceHandler.ConnectionStatus.UNKNOWN
        self.requested_firmware_id = None

        self.loaded_callbacks = []
        self.unloaded_callbacks = []

        self.reset_active_sfd()

    def register_sfd_loaded_callback(self, callback: SFDLoadedCallback):
        self.loaded_callbacks.append(callback)

    def register_sfd_unloaded_callback(self, callback: SFDUnloadedCallback):
        self.unloaded_callbacks.append(callback)

    def init(self):
        self.reset_active_sfd()

    def close(self):
        pass

    def set_autoload(self, val: bool) -> None:
        self.autoload = val

    # To be called periodically
    def process(self):
        device_status = self.device_handler.get_connection_status()

        if self.autoload:
            if device_status != DeviceHandler.ConnectionStatus.CONNECTED_READY:
                self.reset_active_sfd()     # Clear active SFD
            else:
                if self.sfd is None:    # if none loaded
                    verbose = self.previous_device_status != device_status
                    device_id = self.device_handler.get_device_id()
                    if device_id is not None:
                        self.load_sfd(device_id, verbose=verbose)   # Initiale loading. Will populate the datastore
                    else:
                        self.logger.critical('No device ID available when connected. This should not happen')

        if self.requested_firmware_id is not None:  # If the API requested to load an SFD
            self.load_sfd(self.requested_firmware_id)
            self.requested_firmware_id = None

        self.previous_device_status = device_status

    def request_load_sfd(self, firmware_id: str) -> None:
        if not SFDStorage.is_installed(firmware_id):
            raise Exception('Firmware ID %s is not installed' % firmware_id)

        self.requested_firmware_id = firmware_id

    def load_sfd(self, firmware_id: str, verbose=True) -> None:
        self.sfd = None
        self.datastore.clear()

        if SFDStorage.is_installed(firmware_id):
            self.logger.info('Loading firmware description file (SFD) for firmware ID %s' % firmware_id)
            self.sfd = SFDStorage.get(firmware_id)

            # populate datastore
            for fullname, vardef in self.sfd.get_vars_for_datastore():
                entry = DatastoreEntry(entry_type=DatastoreEntry.EntryType.Var, display_path=fullname, variable_def=vardef)
                try:
                    self.datastore.add_entry(entry)
                except Exception as e:
                    self.logger.warning('Cannot add entry "%s". %s' % (fullname, str(e)))
                    self.logger.debug(traceback.format_exc())

            for callback in self.loaded_callbacks:
                try:
                    callback.__call__(self.sfd)
                except Exception as e:
                    self.logger.critical('Error in SFD Load callback. %s' % str(e))
                    self.logger.debug(traceback.format_exc())

        else:
            if verbose:
                self.logger.warning('No SFD file installed for device with firmware ID %s' % firmware_id)

    def get_loaded_sfd(self) -> Optional[FirmwareDescription]:
        return self.sfd

    def reset_active_sfd(self) -> None:
        must_call_callback = (self.sfd is not None)

        self.sfd = None
        self.datastore.clear()
        if must_call_callback:
            self.logger.debug('Triggering SFD Unload callback')
            for callback in self.unloaded_callbacks:
                try:
                    callback.__call__()
                except Exception as e:
                    self.logger.critical('Error in SFD Unload callback. %s' % str(e))
                    self.logger.debug(traceback.format_exc())
