# -- coding: utf-8 --

"""Hikvision MVS SDK: enumerate, open, grab, convert to BGR for UNet++ live streaming."""

import os
import platform
import sys
import threading
import time
from ctypes import *

try:
    from queue import Full, Queue
except ImportError:
    from Queue import Full, Queue

import cv2
import numpy

from . import config

currentsystem = platform.system()
if currentsystem == "Windows":
    sys.path.append(os.getenv("MVCAM_COMMON_RUNENV") + "/Samples/Python/MvImport")
else:
    sys.path.append("/opt/MVS/Samples/aarch64/Python/MvImport")

from MvCameraControl_class import *  # noqa: F401,F403,E402


AUTO_ENUM = {
    "off": 0,
    "once": 1,
    "continuous": 2,
}


def resize_keep_aspect(image, target_width):
    height, width = image.shape[:2]
    if width <= target_width:
        return image
    scale = float(target_width) / float(width)
    new_height = int(height * scale)
    return cv2.resize(image, (target_width, new_height), interpolation=cv2.INTER_AREA)


def initialize_sdk():
    MvCamera.MV_CC_Initialize()


def finalize_sdk():
    MvCamera.MV_CC_Finalize()


def enum_devices():
    device_list = MV_CC_DEVICE_INFO_LIST()
    tlayer_type = (
        MV_GIGE_DEVICE | MV_USB_DEVICE | MV_GENTL_CAMERALINK_DEVICE |
        MV_GENTL_CXP_DEVICE | MV_GENTL_XOF_DEVICE
    )
    ret = MvCamera.MV_CC_EnumDevices(tlayer_type, device_list)
    if ret != 0:
        raise RuntimeError("enum devices fail! ret[0x%x]" % ret)
    return device_list


def print_device_list(device_list):
    print("Find %d devices!" % device_list.nDeviceNum)
    for i in range(0, device_list.nDeviceNum):
        mvcc_dev_info = cast(device_list.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        print_device_info(i, mvcc_dev_info)


def print_device_info(index, dev_info):
    if dev_info.nTLayerType == MV_GIGE_DEVICE or dev_info.nTLayerType == MV_GENTL_GIGE_DEVICE:
        print("\ngige device: [%d]" % index)
        print("device model name: %s" % chars_to_string(dev_info.SpecialInfo.stGigEInfo.chModelName))
        nip1 = ((dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0xff000000) >> 24)
        nip2 = ((dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0x00ff0000) >> 16)
        nip3 = ((dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0x0000ff00) >> 8)
        nip4 = (dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0x000000ff)
        print("current ip: %d.%d.%d.%d\n" % (nip1, nip2, nip3, nip4))


def chars_to_string(chars):
    return "".join([chr(c) for c in chars if c != 0])


def selected_indices(device_list):
    if config.CAMERA_INDICES is None:
        return list(range(device_list.nDeviceNum))
    valid = []
    for index in config.CAMERA_INDICES:
        if 0 <= index < device_list.nDeviceNum:
            valid.append(index)
        else:
            print("WARNING: camera index %s is outside enumerated range" % index)
    return valid


def open_camera(device_list, index):
    cam = MvCamera()
    st_device_list = cast(device_list.pDeviceInfo[index], POINTER(MV_CC_DEVICE_INFO)).contents

    ret = cam.MV_CC_CreateHandle(st_device_list)
    if ret != 0:
        raise RuntimeError("camera %d create handle fail! ret[0x%x]" % (index, ret))

    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        cam.MV_CC_DestroyHandle()
        raise RuntimeError("camera %d open device fail! ret[0x%x]" % (index, ret))

    configure_camera(cam, st_device_list, index)
    return cam


def configure_camera(cam, st_device_list, camera_id):
    configure_packet_size(cam, st_device_list, camera_id)
    require_enum(cam, camera_id, "TriggerMode", MV_TRIGGER_MODE_OFF)
    apply_auto_adjustment(cam, camera_id)


def configure_packet_size(cam, st_device_list, camera_id):
    if st_device_list.nTLayerType != MV_GIGE_DEVICE and st_device_list.nTLayerType != MV_GENTL_GIGE_DEVICE:
        return
    n_packet_size = cam.MV_CC_GetOptimalPacketSize()
    if int(n_packet_size) > 0:
        ret = cam.MV_CC_SetIntValue("GevSCPSPacketSize", n_packet_size)
        if ret != 0:
            print("WARNING camera %d: Set GevSCPSPacketSize fail! ret[0x%x]" % (camera_id, ret))


def apply_auto_adjustment(cam, camera_id):
    if not config.ENABLE_AUTO_ADJUSTMENT or config.AUTO_ADJUST_MODE == "off":
        return
    mode_value = AUTO_ENUM.get(config.AUTO_ADJUST_MODE, 1)
    print("camera %d: applying auto adjustment mode=%s" % (camera_id, config.AUTO_ADJUST_MODE))
    set_enum_warning(cam, camera_id, "ExposureAuto", mode_value)
    set_enum_warning(cam, camera_id, "GainAuto", mode_value)
    set_enum_warning(cam, camera_id, "BalanceWhiteAuto", mode_value)


def lock_auto_adjustment(cam, camera_id):
    if (
        not config.ENABLE_AUTO_ADJUSTMENT or
        config.AUTO_ADJUST_MODE != "once" or
        not config.LOCK_AUTO_ADJUST_AFTER_ONCE
    ):
        return
    print("camera %d: locking auto adjustment values" % camera_id)
    set_enum_warning(cam, camera_id, "ExposureAuto", AUTO_ENUM["off"])
    set_enum_warning(cam, camera_id, "GainAuto", AUTO_ENUM["off"])
    set_enum_warning(cam, camera_id, "BalanceWhiteAuto", AUTO_ENUM["off"])


def require_enum(cam, camera_id, feature_name, value):
    ret = cam.MV_CC_SetEnumValue(feature_name, value)
    if ret != 0:
        raise RuntimeError("camera %d: set %s fail! ret[0x%x]" % (camera_id, feature_name, ret))


def set_enum_warning(cam, camera_id, feature_name, value):
    ret = cam.MV_CC_SetEnumValue(feature_name, value)
    if ret != 0:
        print("WARNING camera %d: set %s=%s fail! ret[0x%x]" % (camera_id, feature_name, value, ret))


def start_grabbing(cam, camera_id):
    ret = cam.MV_CC_StartGrabbing()
    if ret != 0:
        raise RuntimeError("camera %d start grabbing fail! ret[0x%x]" % (camera_id, ret))
    if config.ENABLE_AUTO_ADJUSTMENT and config.AUTO_ADJUST_MODE == "once":
        print("camera %d: waiting %.1fs for auto adjustment" % (camera_id, config.AUTO_ADJUST_SETTLE_SECONDS))
        time.sleep(config.AUTO_ADJUST_SETTLE_SECONDS)
        lock_auto_adjustment(cam, camera_id)


def close_camera(cam, camera_id):
    ret = cam.MV_CC_StopGrabbing()
    if ret != 0:
        print("WARNING camera %d: stop grabbing fail! ret[0x%x]" % (camera_id, ret))
    ret = cam.MV_CC_CloseDevice()
    if ret != 0:
        print("WARNING camera %d: close device fail! ret[0x%x]" % (camera_id, ret))
    ret = cam.MV_CC_DestroyHandle()
    if ret != 0:
        print("WARNING camera %d: destroy handle fail! ret[0x%x]" % (camera_id, ret))


def is_hb_pixel_format(en_pixel_type=0):
    return en_pixel_type in (
        PixelType_Gvsp_HB_Mono8, PixelType_Gvsp_HB_Mono10, PixelType_Gvsp_HB_Mono10_Packed,
        PixelType_Gvsp_HB_Mono12, PixelType_Gvsp_HB_Mono12_Packed, PixelType_Gvsp_HB_Mono16,
        PixelType_Gvsp_HB_RGB8_Packed, PixelType_Gvsp_HB_BGR8_Packed, PixelType_Gvsp_HB_RGBA8_Packed,
        PixelType_Gvsp_HB_BGRA8_Packed, PixelType_Gvsp_HB_RGB16_Packed, PixelType_Gvsp_HB_BGR16_Packed,
        PixelType_Gvsp_HB_RGBA16_Packed, PixelType_Gvsp_HB_BGRA16_Packed, PixelType_Gvsp_HB_YUV422_Packed,
        PixelType_Gvsp_HB_YUV422_YUYV_Packed, PixelType_Gvsp_HB_BayerGR8, PixelType_Gvsp_HB_BayerRG8,
        PixelType_Gvsp_HB_BayerGB8, PixelType_Gvsp_HB_BayerBG8, PixelType_Gvsp_HB_BayerRBGG8,
        PixelType_Gvsp_HB_BayerGB10, PixelType_Gvsp_HB_BayerGB10_Packed, PixelType_Gvsp_HB_BayerBG10,
        PixelType_Gvsp_HB_BayerBG10_Packed, PixelType_Gvsp_HB_BayerRG10, PixelType_Gvsp_HB_BayerRG10_Packed,
        PixelType_Gvsp_HB_BayerGR10, PixelType_Gvsp_HB_BayerGR10_Packed, PixelType_Gvsp_HB_BayerGB12,
        PixelType_Gvsp_HB_BayerGB12_Packed, PixelType_Gvsp_HB_BayerBG12, PixelType_Gvsp_HB_BayerBG12_Packed,
        PixelType_Gvsp_HB_BayerRG12, PixelType_Gvsp_HB_BayerRG12_Packed, PixelType_Gvsp_HB_BayerGR12,
        PixelType_Gvsp_HB_BayerGR12_Packed
    )


def is_mono_pixel_format(en_pixel_type=0):
    return en_pixel_type in (
        PixelType_Gvsp_Mono8, PixelType_Gvsp_Mono10, PixelType_Gvsp_Mono10_Packed,
        PixelType_Gvsp_Mono12, PixelType_Gvsp_Mono12_Packed, PixelType_Gvsp_Mono14, PixelType_Gvsp_Mono16
    )


def frame_to_bgr(cam, st_out_frame):
    st_decode_param = MV_CC_HB_DECODE_PARAM()
    st_convert_param = MV_CC_PIXEL_CONVERT_PARAM_EX()
    memset(byref(st_convert_param), 0, sizeof(st_convert_param))

    if is_hb_pixel_format(st_out_frame.stFrameInfo.enPixelType):
        decode_buffer_len = st_out_frame.stFrameInfo.nWidth * st_out_frame.stFrameInfo.nHeight * 3
        decode_buffer = (c_ubyte * decode_buffer_len)()
        st_decode_param.pSrcBuf = st_out_frame.pBufAddr
        st_decode_param.nSrcLen = st_out_frame.stFrameInfo.nFrameLen
        st_decode_param.pDstBuf = decode_buffer
        st_decode_param.nDstBufSize = decode_buffer_len
        ret = cam.MV_CC_HBDecode(st_decode_param)
        if ret != 0:
            raise RuntimeError("HB Decode fail! ret[0x%x]" % ret)
        st_convert_param.pSrcData = st_decode_param.pDstBuf
        st_convert_param.nSrcDataLen = st_decode_param.nDstBufLen
        st_convert_param.enSrcPixelType = st_decode_param.enDstPixelType
    else:
        st_convert_param.pSrcData = st_out_frame.pBufAddr
        st_convert_param.nSrcDataLen = st_out_frame.stFrameInfo.nFrameLen
        st_convert_param.enSrcPixelType = st_out_frame.stFrameInfo.enPixelType

    if is_mono_pixel_format(st_convert_param.enSrcPixelType):
        dst_pixel_type = PixelType_Gvsp_Mono8
        channel_count = 1
    else:
        dst_pixel_type = PixelType_Gvsp_RGB8_Packed
        channel_count = 3

    dst_buffer_len = channel_count * st_out_frame.stFrameInfo.nWidth * st_out_frame.stFrameInfo.nHeight
    dst_buffer = (c_ubyte * dst_buffer_len)()
    st_convert_param.nWidth = st_out_frame.stFrameInfo.nWidth
    st_convert_param.nHeight = st_out_frame.stFrameInfo.nHeight
    st_convert_param.enDstPixelType = dst_pixel_type
    st_convert_param.pDstBuffer = dst_buffer
    st_convert_param.nDstBufferSize = dst_buffer_len

    ret = cam.MV_CC_ConvertPixelTypeEx(st_convert_param)
    if ret != 0:
        raise RuntimeError("convert pixel fail! ret[0x%x]" % ret)

    if channel_count == 1:
        numpy_image = numpy.frombuffer(dst_buffer, dtype=numpy.ubyte, count=dst_buffer_len).reshape(
            st_out_frame.stFrameInfo.nHeight, st_out_frame.stFrameInfo.nWidth
        )
        return cv2.cvtColor(numpy_image, cv2.COLOR_GRAY2BGR)

    numpy_image = numpy.frombuffer(dst_buffer, dtype=numpy.ubyte, count=dst_buffer_len).reshape(
        st_out_frame.stFrameInfo.nHeight, st_out_frame.stFrameInfo.nWidth, 3
    )
    return cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)


def camera_loop(camera_id, cam, frame_store, stop_event):
    """Per-camera grab loop. UNet runs in inference_worker; RTSP in stream_worker."""
    st_out_frame = MV_FRAME_OUT()
    memset(byref(st_out_frame), 0, sizeof(st_out_frame))

    frame_count = 0
    total_frame_count = 0
    fps_start_time = time.time()
    current_fps = 0.0

    save_queue = None
    save_thread = None
    camera_save_dir = None
    if config.ENABLE_IMAGE_SAVE:
        camera_save_dir = os.path.join(config.IMAGE_SAVE_DIR, "cam_%d" % camera_id)
        os.makedirs(camera_save_dir, exist_ok=True)
        save_queue = Queue(maxsize=8)
        save_thread = threading.Thread(target=_image_save_loop, args=(camera_id, save_queue))
        save_thread.daemon = True
        save_thread.start()

    try:
        while not stop_event.is_set():
            ret = cam.MV_CC_GetImageBuffer(st_out_frame, 1000)
            if st_out_frame.pBufAddr is None or ret != 0:
                continue

            try:
                frame_count += 1
                total_frame_count += 1
                now = time.time()
                elapsed = now - fps_start_time
                if elapsed >= 1.0:
                    current_fps = frame_count / elapsed
                    st = frame_store.get_status(camera_id)
                    infer_ms = float(st.get("infer_ms", 0.0))
                    trt_status = st.get("trt_status", "TRT WAITING")
                    print(
                        "cam %d grab FPS: %.2f | infer: %.1f ms | %s | lost pkts: %d" %
                        (camera_id, current_fps, infer_ms, trt_status, st_out_frame.stFrameInfo.nLostPacket)
                    )
                    frame_count = 0
                    fps_start_time = now

                bgr = frame_to_bgr(cam, st_out_frame)
                raw_frame = resize_keep_aspect(bgr, config.DISPLAY_WIDTH)
                preview = raw_frame.copy()
                cv2.putText(
                    preview,
                    "Cam %d grab %.1f fps" % (camera_id, current_fps),
                    (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2,
                )
                frame_store.update_raw(
                    camera_id,
                    raw_frame,
                    preview,
                    {"fps": current_fps},
                )

                if config.ENABLE_IMAGE_SAVE and save_queue is not None:
                    if total_frame_count % config.SAVE_EVERY_N_FRAMES == 0:
                        path = os.path.join(
                            camera_save_dir,
                            "cam_%d_%06d.jpg" % (camera_id, total_frame_count),
                        )
                        params = [int(cv2.IMWRITE_JPEG_QUALITY), config.IMAGE_SAVE_JPEG_QUALITY]
                        try:
                            save_queue.put_nowait((path, raw_frame.copy(), params))
                        except Full:
                            pass

            except Exception as exc:
                print("camera %d processing failed: %s" % (camera_id, exc))
            finally:
                cam.MV_CC_FreeImageBuffer(st_out_frame)
    finally:
        if save_queue is not None:
            save_queue.put(None)
            if save_thread is not None:
                save_thread.join(timeout=3.0)


def _image_save_loop(camera_id, save_queue):
    while True:
        item = save_queue.get()
        try:
            if item is None:
                return
            output_path, frame, imwrite_params = item
            if not cv2.imwrite(output_path, frame, imwrite_params):
                print("WARNING camera %d: save failed: %s" % (camera_id, output_path))
        finally:
            save_queue.task_done()
