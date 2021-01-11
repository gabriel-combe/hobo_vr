"""Various VR utilities."""

import queue
import threading
import time
from asyncio.streams import StreamReader
from typing import Sequence, Dict
import struct

import numpy as np
import serial.threaded
from pykalman import KalmanFilter
from copy import copy

try:
    import cv2
    from displayarray import read_updates
    from displayarray import display

    HAVE_VOD = True

except Exception as e:
    print(f"failed to import displayarray and/or opencv, reason: {e}")
    print("camera based tracking methods will not be available")
    HAVE_VOD = False

from itertools import islice, takewhile
import re
from typing import Optional


def format_str_for_write(input_str: str) -> bytes:
    """Format a string for writing to SteamVR's stream."""
    if len(input_str) < 1:
        return "".encode("utf-8")

    if input_str[-1] != "\n":
        return (input_str + "\n").encode("utf-8")

    return input_str.encode("utf-8")


async def read(reader: StreamReader, read_len: int = 20) -> str:
    """Read one line from reader asynchronously."""
    data = []
    temp = " "
    while "\n" not in temp and temp != "":
        temp = await reader.read(read_len)
        temp = temp.decode("utf-8")
        data.append(temp)
        time.sleep(0)  # allows thread switching

    return "".join(data)


def read2(reader, read_len=20):
    """Read one line from reader asynchronously."""
    data = []
    temp = " "
    while "\n" not in temp and temp != "":
        temp = reader.recv(read_len)
        temp = temp.decode("utf-8")
        data.append(temp)
        time.sleep(0)  # allows thread switching

    return "".join(data)


async def read3(reader: StreamReader, read_len: int = 20) -> str:
    """Read one line from reader asynchronously."""
    data = bytearray()
    temp = b" "
    while b"\n" not in temp and temp != b"":
        temp = await reader.read(read_len)
        data.extend(temp)
        # time.sleep(0)  # allows thread switching

    return data


def make_rotmat(angls, dtype=np.float64):
    """
    Rotate a set of points around the x, y, then z axes.

    :param points: a point dictionary, such as: [[0, 0, 0], [1, 0, 0]]
    :param angles: the degrees to rotate on the x, y, and z axis
    """
    rotx = np.array(
        [
            [1, 0, 0],
            [0, np.cos(angls[0]), -np.sin(angls[0])],
            [0, np.sin(angls[0]), np.cos(angls[0])],
        ],
        dtype=dtype,
    )

    roty = np.array(
        [
            [np.cos(angls[1]), 0, np.sin(angls[1])],
            [0, 1, 0],
            [-np.sin(angls[1]), 0, np.cos(angls[1])],
        ],
        dtype=dtype,
    )

    rotz = np.array(
        [
            [np.cos(angls[2]), -np.sin(angls[2]), 0],
            [np.sin(angls[2]), np.cos(angls[2]), 0],
            [0, 0, 1],
        ],
        dtype=dtype,
    )

    return np.matmul(np.matmul(rotx, roty), rotz)

def translate(points, offsets: Sequence[float]) -> None:
    """
    Translate a set of points along the x, y, then z axes.

    :param points: a point dictionary, such as: {"marker 1" : np.array((0, 0, 0))}
    :param offsets: the distance to translate on the x, y, and z axes
    """
    points += offsets


def strings_share_characters(str1: str, str2: str) -> bool:
    """Determine if two strings share any characters."""
    for i in str2:
        if i in str1:
            return True

    return False


def get_numbers_from_text(text, separator="\t"):
    """Get a list of number from a string of numbers separated by :separator:[default: "\t"]."""
    try:
        if isinstance(text, bytearray) or isinstance(text, bytes):
            text = text.decode("utf-8")

        if (
                strings_share_characters(
                    text.lower(), "qwrtyuiopsasdfghjklzxcvbnm><*[]{}()"
                )
                or len(text) == 0
        ):
            return []

        return [float(i) for i in text.split(separator)]

    except Exception as e:
        print(f"get_numbers_from_text: {repr(e)} {repr(text)}")

        return []


def get_pose_struct_from_text(text):
    """returns struct from :text:, :text: has to be styled so that it completely matches this regex: ([htc][0-9]+[ ])*([htc][0-9]+)$"""
    res = re.search("([htc][0-9]+[ ])*([htc][0-9]+)$", text)
    if res != None:
        if res.group(0) == text:
            return tuple(i[0] for i in text.split(" ")), tuple(
                int(i[1:]) for i in text.split(" ")
            )
    return (), ()


def parse_poses_from_packet(packet, struct):
    """parses all poses from a :packet:, provided a packet :struct:"""
    it = iter(packet)
    return [tuple(islice(it, 0, i)) for i in struct]


def get_poses_shape(poses):
    """returns a shape of :poses: parsed by parse_poses_from_packet"""
    return tuple(len(i) for i in poses)


def has_nan_in_pose(pose):
    """Determine if any numbers in pose are invalid."""
    return np.isnan(pose).any() or np.isinf(pose).any()


class LazyKalman:
    """
    no docs... too bad.

    But then, SimLeek added some.

    example usage:
        t = LazyKalman([1, 2, 3], np.eye(3), np.eye(3)) # init filter

        for i in range(10):
            print (t.apply([5+i, 6+i, 7+i])) # apply and update filter
    """

    def __init__(
            self, init_state, transition_matrix, observation_matrix, n_iter=5, train_size=15
    ):
        """
        Create the Kalman filter.

        :param init_state: Initial state of the Kalman filter. Should be equal to first element in first_train_batch.
        :param transition_matrix: adjacency matrix representing state transition from t to t+1 for any time t
                                  Example: http://campar.in.tum.de/Chair/KalmanFilter
                                  Most likely, this will be an NxN eye matrix the where N is the number of variables
                                  being estimated
        :param observation_matrix: translation matrix from measurement coordinate system to desired coordinate system
                                   See: https://dsp.stackexchange.com/a/27488
                                   Most likely, this will be an NxN eye matrix the where N is the number of variables
                                   being estimated
        :param n_iter: Number of times to repeat the parameter estimation function (estimates noise)
        """
        init_state = np.array(init_state)
        transition_matrix = np.array(transition_matrix)
        observation_matrix = np.array(observation_matrix)

        self._expected_shape = init_state.shape

        self._filter = KalmanFilter(
            transition_matrices=transition_matrix,
            observation_matrices=observation_matrix,
            initial_state_mean=init_state,
        )

        self.do_learning = True

        self._calibration_countdown = 0
        self._calibration_observations = []
        self._em_iter = n_iter

        self.calibrate(train_size, n_iter)

        self._x_now = np.zeros(3)
        self._p_now = np.zeros(3)

    def apply(self, obz):
        """Apply the Kalman filter against the observation obz."""
        # Todo: automatically start a calibration routine if everything is at rest for long enough,
        #  or if the user leaves, or if there is enough error. Also, add some UI calibrate button, save, and load.

        if self.do_learning:
            self._x_now, self._p_now = self._filter.filter_update(
                filtered_state_mean=self._x_now,
                filtered_state_covariance=self._p_now,
                observation=obz,
            )
        else:
            # print(obz.shape)
            # self._x_now, self._p_now = self._filter.filter([obz,])
            self._x_now, _ = self._filter.filter_update(
                filtered_state_mean=self._x_now,
                filtered_state_covariance=self._p_now,
                observation=obz,
            )

        if self._calibration_countdown:
            self._calibration_observations.append(obz)
            self._calibration_countdown -= 1
            if self._calibration_countdown == 0:
                self._run_calibration()
        return self._x_now

    def _run_calibration(self):
        """Update the Kalman filter so that noise matrices are more accurate."""
        t_start = time.time()
        self._filter = self._filter.em(
            self._calibration_observations, n_iter=self._em_iter
        )
        print(f" kalman filter calibrated, took {time.time() - t_start}s")
        f_means, f_covars = self._filter.filter(self._calibration_observations)
        self._x_now = f_means[-1, :]
        self._p_now = f_covars[-1, :]
        self._calibration_observations = []

    def calibrate(self, observations=100000, em_iter=10):
        """
        Start the calibration routine.

        :param observations: Number of observations before calibration runs.
        :param em_iter: number of times calibration runs over all observations.
        """
        self._calibration_countdown = observations
        self._em_iter = em_iter


class SerialReaderFactory(serial.threaded.LineReader):
    """
    A protocol factory for serial.threaded.ReaderThread.

    self.lastRead should be read only, if you need to modify it make a copy

    usage:
        with serial.threaded.ReaderThread(serial_instance, SerialReaderFactory) as protocol:
            protocol.lastRead # contains the last incoming message from serial
            protocol.write_line(single_line_text) # used to write a single line to serial
    """
    TERMINATOR = b'\n'

    def __init__(self):
        """Create the SerialReaderFactory."""
        super().__init__()
        self._last_read = ""

    @property
    def last_read(self):
        """Get the readonly last read."""
        return self._last_read

    def handle_line(self, data):
        """Store the latest line in last_read."""
        self._last_read = data
        # print (f'cSerialReader: data received: {repr(data)}')

    def connection_lost(self, exc):
        """Notify the user that the connection was lost."""
        print(
            f"SerialReaderFactory: port {repr(self.transport.serial.port)} closed {repr(exc)}"
        )


class SerialReaderBinary(serial.threaded.Packetizer):
    """
    Read binary packets from serial port. Packets are expected to be terminated
    with a TERMINATOR byte (null byte by default).

    The class also keeps track of the transport.
    """

    TERMINATOR = b'\t\r\n'
    ENCODING = 'utf-8'
    UNICODE_HANDLING = 'replace'

    def __init__(self, struct_type='f', struct_len=13, type_len=4):
        super().__init__()
        self._last_packet = None
        self._struct_form = struct_type * struct_len
        self._struct_len = struct_len * type_len

    def __call__(self):
        return self

    @property
    def last_read(self):
        """Get the readonly last read."""
        return self._last_packet

    def connection_lost(self, exc):
        """Notify the user that the connection was lost."""
        print(
            f"SerialReaderBinary: port {repr(self.transport.serial.port)} closed {repr(exc)}"
        )

    def handle_packet(self, packet):
        """Process packets - to be overridden by subclassing"""
        # print (repr(packet))
        if len(packet) == self._struct_len:
            self._last_packet = struct.unpack_from(self._struct_form, packet)

    def write_line(self, text):
        """
        Write text to the transport. ``text`` is a Unicode string and the encoding
        is applied before sending ans also the newline is append.
        """
        # + is not the best choice but bytes does not support % or .format in py3 and we want a single write call
        self.transport.write(text.encode(self.ENCODING, self.UNICODE_HANDLING) + self.TERMINATOR)

class SerialReaderMultiBinary(serial.threaded.Packetizer):
    """
    Read binary packets from serial port. Packets are expected to be terminated
    with a TERMINATOR byte (null byte by default).

    The class also keeps track of the transport.
    """

    HEADINGS = [b'hmd:', b'lc:', b'rc:']
    HEADING_FORMS = {b'hmd:': '4f', b'lc:': '7f5?', b'rc:': '7f5?'}
    TERMINATOR = b'\t\r\n'
    ENCODING = 'utf-8'
    UNICODE_HANDLING = 'replace'

    def __init__(self, struct_type='f', struct_len=13, type_len=4):
        super().__init__()
        self._last_packet = None
        self._struct_form = struct_type * struct_len
        self._struct_len = struct_len * type_len
        self._last_headed_packet = None

    def __call__(self):
        return self

    @property
    def last_read(self):
        """Get the readonly last read."""
        return self._last_packet

    def connection_lost(self, exc):
        """Notify the user that the connection was lost."""
        print(
            f"SerialReaderBinary: port {repr(self.transport.serial.port)} closed {repr(exc)}"
        )
        raise exc

    def data_received(self, data):
        """Buffer received data, find TERMINATOR, call handle_packet"""
        self.buffer.extend(data)
        while self.TERMINATOR in self.buffer:
            packet, self.buffer = self.buffer.split(self.TERMINATOR, 1)
            for h in self.HEADINGS:
                if h in packet:
                    _, p = packet.split(h, 1)
                    self.handle_packet((h,p))

    def handle_packet(self, packet):
        """Process packets - to be overridden by subclassing"""
        # print (repr(packet))
        header = packet[0]
        data = packet[1]
        #print(data)
        if header not in self.HEADINGS:
            return

        form = self.HEADING_FORMS[header]

        offset = 0
        temp_last_packet = [header]
        check = struct.calcsize(form)
        packet_section = data[:check]
        if len(packet_section) != check:
            return
        temp_last_packet.append(struct.unpack_from(form, packet_section))
        self._last_packet = tuple(temp_last_packet)

    def write_line(self, text):
        """
        Write text to the transport. ``text`` is a Unicode string and the encoding
        is applied before sending ans also the newline is append.
        """
        # + is not the best choice but bytes does not support % or .format in py3 and we want a single write call
        self.transport.write(text.encode(self.ENCODING, self.UNICODE_HANDLING) + self.TERMINATOR)


def cnt_2_x_y_w(cnt):
    nc = cnt.reshape((cnt.shape[0], 2))
    return (
        (nc[:, 0].min() + nc[:, 0].max()) / 2,
        (nc[:, 1].min() + nc[:, 1].max()) / 2,
        nc[:, 0].max() - nc[:, 0].min(),
    )

from .blobfinder import BlobFinder

class BlobTracker(threading.Thread):
    """
    Tracks color blobs in 3D space.

    all tracking is done in a separate thread, so use this class in context manager

    self.start() - starts tracking thread
    self.close() - stops tracking thread

    self.get_poses() - gets latest tracker poses
    """

    def __init__(
            self,
            cam_index=0,
            *,
            focal_length_px=490,
            ball_radius_cm=2,
            color_masks=[],
    ):
        """
        Create a blob tracker.

        example usage:
        >>> tracker = BlobTracker(color_masks=[
                ColorRange(98, 10, 200, 55, 250, 32),
                ColorRange(68, 15, 135, 53, 255, 50),
                ColorRange(68, 15, 135, 53, 255, 50),
                                ],
        ... })

        >>> with tracker:
        ...     for _ in range(3):
        ...         poses = tracker.get_poses() # gets latest poses for the trackers
        ...         print (poses)
        ...         time.sleep(1/60) # some delay, doesn't affect the tracking
        array([[0, 0, 0]])
        array([[0, 0, 0]])
        array([[0, 0, 0]])

        :param cam_index: index of the camera that will be used to track the blobs
        :param focal_length_px: focal length in pixels
        :param ball_radius_cm: the radius of the ball to be tracked in cm
        :param color_masks: color mask parameters, in opencv hsv color space, for color detection
        """
        if not HAVE_VOD:
            raise RuntimeError(
                "displayarray is not installed, no camera based tracking methods are available"
            )

        super().__init__()
        self.cam_index = cam_index
        self._vs: Optional[display] = None
        self.color_masks = color_masks
        self.blob_finders = [BlobFinder(c) for c in color_masks]

        self.camera_focal_length = focal_length_px
        self.BALL_RADIUS_CM = ball_radius_cm
        self.cam_index = cam_index

        self.last_frame = None
        self.time_of_last_frame = -1

        self.frame_height, self.frame_width = None, None
        self.markerMasks = np.array([[i.hue_center, i.hue_range, i.sat_center, i.sat_range, i.val_center, i.val_range] for i in color_masks], dtype=np.float64)

        self.poses = np.zeros((self.markerMasks.shape[0], 3), dtype=np.float64)
        self.blobs = [None for _ in range(self.markerMasks.shape[0])]

        self.kalmanFilterz = [None for _ in range(self.markerMasks.shape[0])]
        self.kalmanFilterz2 = [None for _ in range(self.markerMasks.shape[0])]

        # -------------------threading related------------------------

        self.alive = True
        self._lock = threading.Lock()
        self.daemon = False
        self.pose_que = queue.Queue()

    def at_start(self) -> None:
        self._vs = read_updates(self.cam_index)

        frame, self.can_track = self._try_get_frame()

        if not self.can_track:
            # self._vs.release()
            self._vs.end()
            self._vs = None
            raise RuntimeError("invalid video source")

        self.frame_height, self.frame_width, _ = frame.shape

    def set_kalman_learning(self, do_learning):
        for i in range(self.markerMasks.shape[0]):
            if self.kalmanFilterz[i] is not None:
                self.kalmanFilterz[i].do_learning = do_learning
            if self.kalmanFilterz2[i] is not None:
                self.kalmanFilterz2[i].do_learning = do_learning

    def _try_get_frame(self):
        try:
            self._vs.update()

            # view may return empty dicts in between frames and before camera initializes.
            # Wait until we receive a frame.
            t0 = time.time()
            while not self._vs.frames and time.time() - t0 < 3.0:
                self._vs.update()
            frame = self._vs.frames[str(self.cam_index)][0]
            can_track = True
            # if frame is not self.last_frame:
            #     self.time_of_last_frame = time.time()
        except Exception as e:
            print("_try_get_frame() failed with:", repr(e), self._vs.frames)
            frame = None
            can_track = False
        # can_track, frame = self._vs.read()

        return frame, can_track

    def run(self):
        """Run the main blob tracking thread."""
        self.at_start()

        try:
            _, self.can_track = self._try_get_frame()

            if not self.can_track:
                raise RuntimeError("video source already expired")

        except Exception as e:
            print(f"failed to start thread, video source expired?: {repr(e)}")
            self.alive = False
            return

        while self.alive and self.can_track:
            try:
                frame, self.can_track = self._try_get_frame()
                self.solve_blob_poses(frame)
                time.sleep(0)
                # rotate(self.poses, self.offsets)

                if not self.can_track:
                    break

            except Exception as e:
                print(f"tracking failed: {repr(e)}")
                break

        self.alive = False
        self.can_track = False

    def get_poses(self):
        """Get last poses."""
        return self.poses.copy()

    def solve_blob_poses(self, image):
        """Solve for and set the poses of all blobs visible by the camera."""
        for key, b in enumerate(self.blob_finders):
            blob = b.find_blob(image)
            if blob is not None:
                center, radius = blob
                x, y = center
                w = radius
                print(x,y,w)

                f_px = self.camera_focal_length
                x_px = x
                y_px = y
                a_px = w / 2

                x_px -= self.frame_width / 2
                y_px = self.frame_height / 2 - y_px

                l_px = np.linalg.norm([x_px, y_px])

                k = l_px / f_px

                j = (l_px + a_px) / f_px

                l = (j - k) / (1 + j * k)

                d_cm = self.BALL_RADIUS_CM * np.sqrt(1 + l ** 2, dtype=np.clongdouble) / l

                fl = np.divide(f_px, l_px, dtype=np.clongdouble)

                z_cm = d_cm * fl / np.sqrt(1 + fl ** 2, dtype=np.clongdouble)

                l_cm = z_cm * k

                x_cm = l_cm * x_px / l_px
                y_cm = l_cm * y_px / l_px

                self.poses[key] = np.array((x_cm / 100, y_cm / 100, z_cm / 100), dtype=np.clongdouble)

                if not has_nan_in_pose(self.poses[key]):
                    if self.kalmanFilterz[key] is None:
                        self.kalmanFilterz[key] = LazyKalman(
                            self.poses[key], np.eye(3), np.eye(3)
                        )
                    else:
                        self.poses[key] = self.kalmanFilterz[key].apply(
                            self.poses[key]
                        )

    def stop(self):
        """Stop the blob tracking thread."""
        self.alive = False
        self.can_track = False
        self.join(4)

    def close(self):
        """Close the blob tracker thread."""
        with self._lock:
            self.stop()

            if self._vs is not None:
                # self._vs.release()
                self._vs.end()
                del self._vs
                self._vs = None

    def __enter__(self):
        self.start()
        if not self.alive:
            self.close()
            raise RuntimeError("video source already expired")

        return self

    def __exit__(self, *exc):
        self.close()
