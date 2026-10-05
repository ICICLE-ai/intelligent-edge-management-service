"""Optional local X11 preview (cv2.imshow). Used only when SHOW_WINDOW=true."""

import time

import cv2


def display_loop(frame_store, stop_event):
    print("Display worker: started (q or Esc closes the windows).")
    windows = {}
    try:
        while not stop_event.is_set():
            camera_ids = frame_store.camera_ids()
            for camera_id in camera_ids:
                name = "cam-%d" % camera_id
                frame = frame_store.get(camera_id, "processed")
                if frame is None:
                    continue
                if name not in windows:
                    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
                    windows[name] = True
                vis = frame
                if vis.shape[1] > 1280:
                    scale = 1280.0 / float(vis.shape[1])
                    vis = cv2.resize(
                        vis,
                        (1280, max(1, int(round(vis.shape[0] * scale)))),
                    )
                cv2.imshow(name, vis)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                print("Display worker: window closed.")
                stop_event.set()
                break
            if not camera_ids:
                time.sleep(0.05)
    except Exception as exc:
        print("Display worker failed: %s" % exc)
        print("Hint: run on the Jetson desktop, xhost +local:docker, and pass -e DISPLAY -v /tmp/.X11-unix")
    finally:
        cv2.destroyAllWindows()
