from devlib.instrument import (Instrument, CONTINUOUS,
                               MeasurementsCsv, MeasurementType)
from devlib.utils.rendering import (GfxinfoFrameCollector,
                                    SurfaceFlingerFrameCollector,
                                    SurfaceFlingerFrame,
                                    read_gfxinfo_columns)


class FramesInstrument(Instrument):

    mode = CONTINUOUS
    collector_cls = None

    def __init__(self, target, collector_target, period=2, keep_raw=True):
        super(FramesInstrument, self).__init__(target)
        self.collector_target = collector_target
        self.period = period
        self.keep_raw = keep_raw
        self.sample_rate_hz = 1 / self.period
        self.collector = None
        self.header = None
        self._need_reset = True
        self._raw_file = None
        self._init_channels()

    def reset(self, sites=None, kinds=None, channels=None):
        super(FramesInstrument, self).reset(sites, kinds, channels)
        self.collector = self.collector_cls(self.target, self.period,
                                            self.collector_target, self.header)
        self._need_reset = False
        self._raw_file = None

    def start(self):
        if self._need_reset:
            self.reset()
        self.collector.start()

    def stop(self):
        self.collector.stop()
        self._need_reset = True

    def get_data(self, outfile):
        if self.keep_raw:
            self._raw_file = outfile + '.raw'
        self.collector.process_frames(self._raw_file)
        active_sites = [chan.label for chan in self.active_channels]
        self.collector.write_frames(outfile, columns=active_sites)
        return MeasurementsCsv(outfile, self.active_channels, self.sample_rate_hz)

    def get_raw(self):
        return [self._raw_file] if self._raw_file else []

    def _init_channels(self):
        raise NotImplementedError()


class GfxInfoFramesInstrument(FramesInstrument):

    mode = CONTINUOUS
    collector_cls = GfxinfoFrameCollector

    def _init_channels(self):
        columns = read_gfxinfo_columns(self.target)
        for entry in columns:
            if entry == 'Flags':
                self.add_channel('Flags', MeasurementType('flags', 'flags'))
            else:
                self.add_channel(entry, 'time_us')
        self.header = [chan.label for chan in self.channels.values()]


class SurfaceFlingerFramesInstrument(FramesInstrument):

    mode = CONTINUOUS
    collector_cls = SurfaceFlingerFrameCollector

    def _init_channels(self):
        for field in SurfaceFlingerFrame._fields:
            # remove the "_time" from filed names to avoid duplication
            self.add_channel(field[:-5], 'time_us')
        self.header = [chan.label for chan in self.channels.values()]
