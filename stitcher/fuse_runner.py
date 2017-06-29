import re
import sys
import os.path
import argparse
import threading

from queue import Queue
from functools import lru_cache

import psutil
import numpy as np
import skimage.external.tifffile as tiff

from .filematrix import FileMatrix
from .inputfile import InputFile
from .fuse import fuse_queue
from .lcd_numbers import numbers


def to_dtype(x, dtype):
    if np.issubdtype(dtype, np.integer):
        np.rint(x, x)
    return x.astype(dtype, copy=False)


class FuseRunner(object):
    def __init__(self, input_file=None):
        self.input_file = input_file  #: input file or folder
        self.fm = None  #: :class:`FileMatrix`
        self.path = None

        self.zmin = 0
        self.zmax = None
        self.debug = False
        self.compute_average = False
        self.output_filename = None

        self._is_multichannel = None

        self._load_df()

    def _load_df(self):
        if os.path.isdir(self.input_file):
            input_file = os.path.join(self.input_file, 'stitch.yml')
        else:
            input_file = self.input_file

        self.path, file_name = os.path.split(input_file)
        self.fm = FileMatrix()
        self.fm.compute_average = self.compute_average
        self.fm.load_yaml(input_file)
        self.fm.process_data()

    @property
    @lru_cache()
    def dtype(self):
        with InputFile(self.fm.data_frame.iloc[0].name) as f:
            return np.dtype(f.dtype)

    @property
    @lru_cache()
    def is_multichannel(self):
        with InputFile(self.fm.data_frame.iloc[0].name) as f:
            if f.nchannels > 1:
                multichannel = True
            else:
                multichannel = False
        return multichannel

    @property
    def output_shape(self):
        thickness = self.fm.full_thickness
        if self.zmax is not None:
            thickness -= (thickness - self.zmax)
        thickness -= self.zmin

        with InputFile(self.fm.data_frame.iloc[0].name) as f:
            output_shape = list(f.shape)

        output_shape[0] = thickness
        output_shape[-2] = self.fm.full_height
        output_shape[-1] = self.fm.full_width

        return output_shape

    def run(self):
        df = self.fm.data_frame
        for key in ['Xs', 'Ys', 'Zs']:
            df[key] -= df[key].min()

        total_byte_size = np.asscalar(np.prod(self.output_shape)
                                      * self.dtype.itemsize)
        bigtiff = True if total_byte_size > 0.95 * 2**32 else False

        ram = psutil.virtual_memory().total

        # size in bytes of an xy plane (including channels) (float32)
        xy_size = np.asscalar(np.prod(self.output_shape[1::]) * 4)
        n_frames_in_ram = int(ram / xy_size / 1.5)

        n_loops = self.output_shape[0] // n_frames_in_ram

        partial_thickness = [n_frames_in_ram for i in range(0, n_loops)]
        remainder = self.output_shape[0] % n_frames_in_ram
        if remainder:
            partial_thickness += [remainder]

        try:
            os.remove(self.output_filename)
        except FileNotFoundError:
            pass

        for thickness in partial_thickness:
            self.zmax = self.zmin + thickness
            fused = np.zeros(self.output_shape, dtype=np.float32)
            q = Queue(maxsize=20)

            t = threading.Thread(target=fuse_queue,
                                 args=(q, fused, self.debug))
            t.start()

            for index, row in self.fm.data_frame.iterrows():
                if self.zmax is None:
                    z_to = row.nfrms
                else:
                    z_to = self.zmax - row.Zs

                if z_to > row.nfrms:
                    z_to = row.nfrms

                if z_to <= 0:
                    continue

                z_from = self.zmin - row.Zs

                if z_from < 0:
                    z_from = 0

                if z_from >= z_to:
                    continue

                with InputFile(os.path.join(self.path, index)) as f:
                    print('opening {}\tz=[{}:{}]'.format(index, z_from, z_to))
                    slice = f.slice(z_from, z_to, dtype=np.float32, copy=True)

                if self.debug:
                    self.overlay_debug(slice, index, z_from)

                top_left = [row.Zs + z_from - self.zmin, row.Ys, row.Xs]
                overlaps = self.fm.overlaps(index).copy()
                overlaps = overlaps.loc[
                    (overlaps['Z_from'] <= z_to) & (overlaps['Z_to'] >= z_from)
                ]

                overlaps['Z_from'] -= z_from
                overlaps['Z_to'] -= z_from

                overlaps.loc[overlaps['Z_from'] < 0, 'Z_from'] = 0

                q.put([slice, top_left, overlaps])

            q.put([None, None, None])  # close queue

            t.join()  # wait for fuse thread to finish
            print('=================================')

            if self.is_multichannel:
                fused = np.moveaxis(fused, -3, -1)

            fused = to_dtype(fused, self.dtype)
            tiff.imsave(self.output_filename, fused, append=True,
                        bigtiff=bigtiff)

            self.zmin += thickness

    def overlay_debug(self, slice, index, z_from):
        cx = slice.shape[-1] // 2
        cy = slice.shape[-2] // 2 + 10
        x = cx - 100
        for xstr in re.findall(r'\d+', index):
            for l in xstr:
                x_end = x + 30
                try:
                    slice[..., cy:cy + 50, x:x_end] = numbers[int(l)]
                except ValueError:
                    break
                x = x_end + 5
            x = x_end + 15

        for f in range(0, slice.shape[0]):
            x = cx - 120
            xstr = str(z_from + f)
            for l in xstr:
                x_end = x + 30
                try:
                    slice[f, ..., cy + 55:cy + 105, x:x_end] = \
                        numbers[int(l)]
                except ValueError:
                    break
                x = x_end + 5


def parse_args():
    parser = argparse.ArgumentParser(
        description='Fuse stitched tiles in a folder.',
        epilog='Author: Giacomo Mazzamuto <mazzamuto@lens.unifi.it>',
        formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('input_file', help='input file (.yml) or folder')

    parser.add_argument('-o', type=str, default='fused.tif',
                        dest='output_filename', help='output file name')

    parser.add_argument('-a', action='store_true', dest='compute_average',
                        help='instead of maximum score, take the average '
                             'result weighted by the score')

    parser.add_argument('-d', dest='debug', action='store_true',
                        help='overlay debug info')

    parser.add_argument('--zmin', type=int, default=0)
    parser.add_argument('--zmax', type=int, default=None, help='noninclusive')

    return parser.parse_args(sys.argv[1:])


def main():
    arg = parse_args()
    fr = FuseRunner(arg.input_file)

    keys = ['zmin', 'zmax', 'output_filename', 'debug', 'compute_average']
    for k in keys:
        setattr(fr, k, getattr(arg, k))

    fr.run()


if __name__ == '__main__':
    main()
