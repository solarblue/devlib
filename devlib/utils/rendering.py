import csv
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import namedtuple, OrderedDict
from distutils.version import LooseVersion

from devlib.exception  import WorkerThreadError, TargetNotRespondingError, TimeoutError


logger = logging.getLogger('rendering')

SurfaceFlingerFrame = namedtuple('SurfaceFlingerFrame',
                                 'desired_present_time actual_present_time frame_ready_time')


class FrameCollector(threading.Thread):

    def __init__(self, target, period):
        super(FrameCollector, self).__init__()
        self.target = target
        self.period = period
        self.stop_signal = threading.Event()
        self.frames = []

        self.temp_file = None
        self.refresh_period = None
        self.drop_threshold = None
        self.unresponsive_count = 0
        self.last_ready_time = None
        self.exc = None
        self.header = None

    def run(self):
        logger.debug('Surface flinger frame data collection started.')
        try:
            self.stop_signal.clear()
            fd, self.temp_file = tempfile.mkstemp()
            logger.debug('temp file: {}'.format(self.temp_file))
            wfh = os.fdopen(fd, 'wb')
            try:
                while not self.stop_signal.is_set():
                    self.collect_frames(wfh)
                    time.sleep(self.period)
            finally:
                wfh.close()
        except (TargetNotRespondingError, TimeoutError):  # pylint: disable=W0703
            raise
        except Exception, e:  # pylint: disable=W0703
            logger.warning('Exception on collector thread: {}({})'.format(e.__class__.__name__, e))
            self.exc = WorkerThreadError(self.name, sys.exc_info())
        logger.debug('Surface flinger frame data collection stopped.')

    def stop(self):
        self.stop_signal.set()
        self.join()
        if self.unresponsive_count:
            message = 'FrameCollector was unrepsonsive {} times.'.format(self.unresponsive_count)
            if self.unresponsive_count > 10:
                logger.warning(message)
            else:
                logger.debug(message)
        if self.exc:
            raise self.exc  # pylint: disable=E0702

    def process_frames(self, outfile=None):
        if not self.temp_file:
            raise RuntimeError('Attempting to process frames before running the collector')
        with open(self.temp_file) as fh:
            self._process_raw_file(fh)
        if outfile:
            shutil.copy(self.temp_file, outfile)
        os.unlink(self.temp_file)
        self.temp_file = None

    def write_frames(self, outfile, columns=None):
        if columns is None:
            header = self.header
            frames = self.frames
        else:
            indexes = []
            for c in columns:
                if c not in self.header:
                    msg = 'Invalid column "{}"; must be in {}'
                    raise ValueError(msg.format(c, self.header))
                indexes.append(self.header.index(c))
            frames = [[f[i] for i in indexes] for f in self.frames]
            header = columns
        with open(outfile, 'w') as wfh:
            writer = csv.writer(wfh)
            if header:
                writer.writerow(header)
            writer.writerows(frames)

    def collect_frames(self, wfh):
        raise NotImplementedError()

    def clear(self):
        raise NotImplementedError()

    def _process_raw_file(self, fh):
        raise NotImplementedError()


class SurfaceFlingerFrameCollector(FrameCollector):

    def __init__(self, target, period, view, header=None):
        super(SurfaceFlingerFrameCollector, self).__init__(target, period)
        self.view = view
        self.header = header or SurfaceFlingerFrame._fields

    def collect_frames(self, wfh):
        for activity in self.list():
            if activity == self.view:
                wfh.write(self.get_latencies(activity))

    def clear(self):
        self.target.execute('dumpsys SurfaceFlinger --latency-clear ')

    def get_latencies(self, activity):
        cmd = 'dumpsys SurfaceFlinger --latency "{}"'
        return self.target.execute(cmd.format(activity))

    def list(self):
        text = self.target.execute('dumpsys SurfaceFlinger --list')
        return text.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    def _process_raw_file(self, fh):
        text = fh.read().replace('\r\n', '\n').replace('\r', '\n')
        for line in text.split('\n'):
            line = line.strip()
            if line:
                self._process_trace_line(line)

    def _process_trace_line(self, line):
        parts = line.split()
        if len(parts) == 3:
            frame = SurfaceFlingerFrame(*map(int, parts))
            if not frame.frame_ready_time:
                return # "null" frame
            if frame.frame_ready_time <= self.last_ready_time:
                return  # duplicate frame
            if (frame.frame_ready_time - frame.desired_present_time) > self.drop_threshold:
                logger.debug('Dropping bogus frame {}.'.format(line))
                return  # bogus data
            self.last_ready_time = frame.frame_ready_time
            self.frames.append(frame)
        elif len(parts) == 1:
            self.refresh_period = int(parts[0])
            self.drop_threshold = self.refresh_period * 1000
        elif 'SurfaceFlinger appears to be unresponsive, dumping anyways' in line:
            self.unresponsive_count += 1
        else:
            logger.warning('Unexpected SurfaceFlinger dump output: {}'.format(line))


def read_gfxinfo_columns(target):
    output = target.execute('dumpsys gfxinfo --list framestats')
    lines = iter(output.split('\n'))
    for line in lines:
        if line.startswith('---PROFILEDATA---'):
            break
    columns_line = lines.next()
    return columns_line.split(',')[:-1]  # has a trailing ','


class GfxinfoFrameCollector(FrameCollector):

    def __init__(self, target, period, package, header=None):
        super(GfxinfoFrameCollector, self).__init__(target, period)
        self.package = package
        self.header =  None
        self._init_header(header)

    def collect_frames(self, wfh):
        cmd = 'dumpsys gfxinfo {} framestats'
        wfh.write(self.target.execute(cmd.format(self.package)))

    def clear(self):
        pass

    def _init_header(self, header):
        if header is not None:
            self.header = header
        else:
            self.header = read_gfxinfo_columns(self.target)

    def _process_raw_file(self, fh):
        found = False
        try:
            while True:
                for line in fh:
                    if line.startswith('---PROFILEDATA---'):
                        found = True
                        break

                fh.next()  # headers
                for line in fh:
                    if line.startswith('---PROFILEDATA---'):
                        break
                    self.frames.append(map(int, line.strip().split(',')[:-1]))  # has a trailing ','
        except StopIteration:
            pass
        if not found:
            logger.warning('Could not find frames data in gfxinfo output')
            return


def _file_reverse_iter(fh, buf_size=1024):
    fh.seek(0, os.SEEK_END)
    offset = 0
    file_size = remaining_size = fh.tell()
    while remaining_size > 0:
        offset = min(file_size, offset + buf_size)
        fh.seek(file_size - offset)
        buf = fh.read(min(remaining_size, buf_size))
        remaining_size -= buf_size
        yield buf


def gfxinfo_get_last_dump(filepath):
    """
    Return the last gfxinfo dump from the frame collector's raw output.

    """
    record = ''
    with open(filepath, 'r') as fh:
        fh_iter = _file_reverse_iter(fh)
        try:
            while True:
                buf = fh_iter.next()
                ix = buf.find('** Graphics')
                if ix >= 0:
                    return buf[ix:] + record

                ix = buf.find(' **\n')
                if ix >= 0:
                    buf =  fh_iter.next() + buf
                    ix = buf.find('** Graphics')
                    if ix < 0:
                        msg = '"{}" appears to be corrupted'
                        raise RuntimeError(msg.format(filepath))
                    return buf[ix:] + record
                record = buf + record
        except StopIteration:
            pass
