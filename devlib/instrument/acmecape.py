#pylint: disable=attribute-defined-outside-init
from __future__ import division
import csv
import os
import time
import tempfile
from fcntl import fcntl, F_GETFL, F_SETFL
from string import Template
from subprocess import Popen, PIPE, STDOUT

from devlib import Instrument, CONTINUOUS, MeasurementsCsv
from devlib.exception import HostError
from devlib.utils.misc import which

OUTPUT_CAPTURE_FILE = 'acme-cape.csv'
IIOCAP_CMD_TEMPLATE = Template("""
${iio_capture} -n ${host} -b ${buffer_size} -c -f ${outfile} ${iio_device}
""")

def _read_nonblock(pipe, size=1024):
    fd = pipe.fileno()
    flags = fcntl(fd, F_GETFL)
    flags |= os.O_NONBLOCK
    fcntl(fd, F_SETFL, flags)

    output = ''
    try:
        while True:
            output += pipe.read(size)
    except IOError:
        pass
    return output


class AcmeCapeInstrument(Instrument):

    mode = CONTINUOUS

    def __init__(self, target,
                 iio_capture=which('iio_capture'),
                 host='baylibre-acme.local',
                 iio_device='iio:device0',
                 buffer_size=256):
        super(AcmeCapeInstrument, self).__init__(target)
        self.iio_capture = iio_capture
        self.host = host
        self.iio_device = iio_device
        self.buffer_size = buffer_size
        self.sample_rate_hz = 100
        if self.iio_capture is None:
            raise HostError('Missing iio-capture binary')
        self.command = None
        self.process = None

        self.add_channel('shunt', 'voltage')
        self.add_channel('bus', 'voltage')
        self.add_channel('device', 'power')
        self.add_channel('device', 'current')
        self.add_channel('timestamp', 'time_ms')

    def reset(self, sites=None, kinds=None, channels=None):
        super(AcmeCapeInstrument, self).reset(sites, kinds, channels)
        self.raw_data_file = tempfile.mkstemp('.csv')[1]
        params = dict(
            iio_capture=self.iio_capture,
            host=self.host,
            buffer_size=self.buffer_size,
            iio_device=self.iio_device,
            outfile=self.raw_data_file
        )
        self.command = IIOCAP_CMD_TEMPLATE.substitute(**params)
        self.logger.debug('ACME cape command: {}'.format(self.command))

    def start(self):
        self.process = Popen(self.command.split(), stdout=PIPE, stderr=STDOUT)

    def stop(self):
        self.process.terminate()
        timeout_secs = 10
        for _ in xrange(timeout_secs):
            if self.process.poll() is not None:
                break
            time.sleep(1)
        else:
            output = _read_nonblock(self.process.stdout)
            self.process.kill()
            self.logger.error('iio-capture did not terminate gracefully')
            if self.process.poll() is None:
                msg = 'Could not terminate iio-capture:\n{}'
                raise HostError(msg.format(output))
        if not os.path.isfile(self.raw_data_file):
            raise HostError('Output CSV not generated.')

    def get_data(self, outfile):
        if os.stat(self.raw_data_file).st_size == 0:
            self.logger.warning('"{}" appears to be empty'.format(self.raw_data_file))
            return

        all_channels = [c.label for c in self.list_channels()]
        active_channels = [c.label for c in self.active_channels]
        active_indexes = [all_channels.index(ac) for ac in active_channels]

        with open(self.raw_data_file, 'rb') as fh:
            with open(outfile, 'wb') as wfh:
                writer = csv.writer(wfh)
                writer.writerow(active_channels)

                reader = csv.reader(fh, skipinitialspace=True)
                header = reader.next()
                ts_index = header.index('timestamp ms')


                for row in reader:
                    output_row = []
                    for i in active_indexes:
                        if i == ts_index:
                            # Leave time in ms
                            output_row.append(float(row[i]))
                        else:
                            # Convert rest into standard units.
                            output_row.append(float(row[i])/1000)
                    writer.writerow(output_row)
        return MeasurementsCsv(outfile, self.active_channels, self.sample_rate_hz)

    def get_raw(self):
        return [self.raw_data_file]
