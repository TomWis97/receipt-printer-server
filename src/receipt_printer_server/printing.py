"""Owning the printer: one job at a time, recovery between jobs.

Every job is first rendered into a memory buffer. That catches everything that
depends only on the content (an unprintable character, a malformed barcode,
an unknown tag) before any paper moves. Only then is the job sent for real.
"""

import logging
import threading

from epson_tm_t88v import (
    TM_T88V_58MM,
    TM_T88V_80MM,
    BufferTransport,
    Printer,
    PrinterError,
    ReadTimeout,
    TransportError,
    WriteTimeout,
)

from .markup import print_markup

log = logging.getLogger(__name__)

PROFILES = {"80mm": TM_T88V_80MM, "58mm": TM_T88V_58MM}

# How long the printer may take to finish a job after the last byte was sent.
COMPLETION_TIMEOUT = 60.0


class PrinterUnavailable(Exception):
    """The job is fine, but the printer cannot print it right now."""


class PrinterService:
    def __init__(self, device=None, paper="80mm", dry_run=False):
        self.device = device
        self.profile = PROFILES[paper]
        self.dry_run = dry_run
        self.lock = threading.Lock()
        # Kept open between jobs, so a job that failed half-way is remembered
        # and the next one can repair the command stream first.
        self.printer = None

    def check(self, markup, base_dir):
        """Render a job without printing. Returns the bytes it would send.

        Raises ValueError or PrinterError when the content cannot be printed.
        """
        buffer = BufferTransport()
        printer = Printer(buffer, profile=self.profile)
        print_markup(printer, markup, base_dir)
        return buffer.data

    def print(self, markup, base_dir):
        data = self.check(markup, base_dir)
        if self.dry_run:
            log.info("dry run: not sending %d bytes to the printer", len(data))
            return
        with self.lock:
            printer = self.ready_printer()
            try:
                print_markup(printer, markup, base_dir)
                printer.wait_for_completion(timeout=COMPLETION_TIMEOUT)
            except (WriteTimeout, ReadTimeout) as exc:
                # The connection is fine; the printer stalled. Most likely it
                # went offline mid-job - see the driver's FINDINGS.md.
                raise PrinterUnavailable(str(exc))
            except TransportError as exc:
                self.disconnect()
                raise PrinterUnavailable(str(exc))

    def status(self):
        """The printer's status, as reported by the printer itself."""
        if self.dry_run:
            return {"ok": True, "status": "dry run: the printer is not used", "problems": []}
        with self.lock:
            try:
                printer = self.connect()
                status = printer.status()
            except TransportError as exc:
                self.disconnect()
                return {"ok": False, "status": str(exc), "problems": [str(exc)]}
        return {"ok": status.ok, "status": str(status), "problems": status.problems}

    def connect(self):
        if self.printer is None:
            # initialize=False: a printer stuck mid-command would swallow the
            # reset as data. ready_printer() resets it once it is known to listen.
            self.printer = Printer(self.device, profile=self.profile, initialize=False)
        return self.printer

    def disconnect(self):
        if self.printer is not None:
            self.printer.close()
            self.printer = None

    def ready_printer(self):
        """Connect, make sure the printer listens and can print, and reset it."""
        try:
            printer = self.listening_printer()
            printer.status().raise_for_status()
            printer.reset()
            return printer
        except PrinterError as exc:
            raise PrinterUnavailable(str(exc))

    def listening_printer(self):
        try:
            printer = self.connect()
            listening = printer.transport.answers()
        except TransportError:
            # The device node goes stale when the printer is switched off and
            # on again. Reconnect once; a second failure is reported.
            self.disconnect()
            printer = self.connect()
            listening = printer.transport.answers()

        if not listening:
            # A job that failed half-way leaves the printer waiting for the
            # rest of a command; it then ignores everything, queries included.
            log.warning("printer is not answering; resynchronising the command stream")
            if not printer.resync():
                raise PrinterUnavailable(
                    "the printer does not answer. Check the front panel; opening "
                    "and closing the cover clears a false paper-out on this unit."
                )
        return printer
