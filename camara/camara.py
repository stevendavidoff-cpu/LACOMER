#!/usr/bin/env python3
"""
camara.py — Raspberry Pi Camera Module 3 (IMX708) for LACOMER.

Gives the rest of the project one simple object that returns OpenCV-ready
BGR frames, so the barcode reader and the bread recognizer don't need to
know which camera is connected.

The Camera Module 3 is a CSI (ribbon cable) camera driven by libcamera, so
cv2.VideoCapture(0) does NOT work with it. This module uses Picamera2.
If Picamera2 is not available (e.g. testing on a PC), it falls back to a
USB webcam through OpenCV.

Quick use from other scripts:
    from camara.camara import Camara
    with Camara() as cam:
        frame = cam.capturar()          # numpy BGR array, ready for cv2 / pyzbar
        cam.guardar_foto("prueba.jpg")

From the command line (on the Pi):
    python3 camara/camara.py --info               # what camera was found
    python3 camara/camara.py --foto prueba.jpg     # take one photo
    python3 camara/camara.py --vista               # live view (needs a desktop), q = quit
    python3 camara/camara.py --serie data/images/nuevas --cada 2   # photo every 2 s, Ctrl+C stops

Settings are read from catalogo.json -> "camara" (all optional):
    "camara": {
        "resolucion": [2304, 1296],   # IMX708 full-FOV binned mode, fast and sharp
        "enfoque": "continuo",       # "continuo" | "auto" (focus once per photo) | number in metres (fixed)
        "rotacion": 0,                # 0 or 180 (if the camera is mounted upside down)
        "hdr": false                  # Camera Module 3 HDR (lower resolution, helps with glare)
    }
"""

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

RAIZ = Path(__file__).resolve().parent.parent
CATALOGO = RAIZ / "catalogo.json"

CONFIG_DEFECTO = {
    "resolucion": [2304, 1296],
    "enfoque": "continuo",
    "rotacion": 0,
    "hdr": False,
}


def leer_config():
    cfg = dict(CONFIG_DEFECTO)
    try:
        with open(CATALOGO, encoding="utf-8") as f:
            cfg.update(json.load(f).get("camara", {}))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


class Camara:
    def __init__(self, config=None):
        self.cfg = config or leer_config()
        self.tipo = None   # "picamera2" or "usb"
        self._cam = None

    # ---------- start / stop ----------
    def abrir(self):
        try:
            from picamera2 import Picamera2
        except ImportError:
            Picamera2 = None

        if Picamera2 is not None and Picamera2.global_camera_info():
            self._abrir_picamera2(Picamera2)
        else:
            self._abrir_usb()
        return self

    def _abrir_picamera2(self, Picamera2):
        from libcamera import Transform, controls

        if self.cfg.get("hdr"):
            self._activar_hdr(True)

        cam = Picamera2()
        ancho, alto = self.cfg["resolucion"]
        rot = int(self.cfg.get("rotacion", 0)) % 360
        transform = Transform(hflip=1, vflip=1) if rot == 180 else Transform()
        # "RGB888" in Picamera2 is stored B,G,R in memory -> directly usable by OpenCV.
        conf = cam.create_still_configuration(
            main={"size": (ancho, alto), "format": "RGB888"},
            transform=transform,
            buffer_count=2,
        )
        cam.configure(conf)
        cam.start()

        enfoque = self.cfg.get("enfoque", "continuo")
        self._tiene_af = "AfMode" in cam.camera_controls  # Module 3 has autofocus
        if self._tiene_af:
            if enfoque == "continuo":
                cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
            elif enfoque == "auto":
                cam.set_controls({"AfMode": controls.AfModeEnum.Auto})
            else:
                # Fixed distance in metres -> LensPosition is in dioptres (1/m)
                metros = float(enfoque)
                cam.set_controls({
                    "AfMode": controls.AfModeEnum.Manual,
                    "LensPosition": 1.0 / max(metros, 0.05),
                })
        time.sleep(1.0)  # let exposure / white balance settle
        self._cam = cam
        self._controls = controls
        self.tipo = "picamera2"

    def _abrir_usb(self):
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            raise RuntimeError(
                "No camera found. Check the ribbon cable and run: rpicam-hello --list-cameras"
            )
        ancho, alto = self.cfg["resolucion"]
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, ancho)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, alto)
        self._cam = cap
        self._tiene_af = False
        self.tipo = "usb"

    @staticmethod
    def _activar_hdr(activo):
        """Camera Module 3 HDR is switched on the sensor via v4l2-ctl before opening."""
        import glob
        import subprocess
        for dev in glob.glob("/dev/v4l-subdev*"):
            subprocess.run(
                ["v4l2-ctl", "--set-ctrl", f"wide_dynamic_range={1 if activo else 0}", "-d", dev],
                capture_output=True,
            )

    def cerrar(self):
        if self._cam is None:
            return
        if self.tipo == "picamera2":
            self._cam.stop()
            self._cam.close()
            if self.cfg.get("hdr"):
                self._activar_hdr(False)
        else:
            self._cam.release()
        self._cam = None

    def __enter__(self):
        return self.abrir()

    def __exit__(self, *exc):
        self.cerrar()

    # ---------- capture ----------
    def enfocar(self):
        """Run one autofocus cycle (only matters with "enfoque": "auto")."""
        if self.tipo == "picamera2" and self._tiene_af:
            return self._cam.autofocus_cycle()
        return True

    def capturar(self):
        """Return one frame as a BGR numpy array."""
        if self._cam is None:
            self.abrir()
        if self.tipo == "picamera2":
            if self.cfg.get("enfoque") == "auto":
                self.enfocar()
            return self._cam.capture_array("main")
        ok, frame = self._cam.read()
        if not ok:
            raise RuntimeError("Could not read a frame from the USB camera")
        if int(self.cfg.get("rotacion", 0)) % 360 == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        return frame

    def guardar_foto(self, ruta, calidad=92):
        frame = self.capturar()
        ruta = Path(ruta)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(ruta), frame, [cv2.IMWRITE_JPEG_QUALITY, calidad])
        return ruta

    def info(self):
        if self.tipo == "picamera2":
            from picamera2 import Picamera2
            datos = Picamera2.global_camera_info()[0]
            return f"Picamera2: {datos.get('Model')} (autofocus: {'yes' if self._tiene_af else 'no'})"
        return "USB camera through OpenCV"


# ---------- command line ----------
def main():
    p = argparse.ArgumentParser(description="LACOMER camera (Camera Module 3)")
    p.add_argument("--info", action="store_true", help="show which camera was detected")
    p.add_argument("--foto", metavar="ARCHIVO", help="take one photo and save it")
    p.add_argument("--vista", action="store_true", help="live view window (q to quit)")
    p.add_argument("--serie", metavar="CARPETA", help="take photos into a folder until Ctrl+C")
    p.add_argument("--cada", type=float, default=2.0, help="seconds between photos for --serie")
    a = p.parse_args()

    with Camara() as cam:
        print(cam.info())

        if a.foto:
            print("Saved:", cam.guardar_foto(a.foto))

        if a.serie:
            carpeta = Path(a.serie)
            print(f"Taking a photo every {a.cada}s into {carpeta}  (Ctrl+C to stop)")
            try:
                while True:
                    nombre = carpeta / time.strftime("%Y%m%d_%H%M%S.jpg")
                    print("  ", cam.guardar_foto(nombre))
                    time.sleep(a.cada)
            except KeyboardInterrupt:
                print("\nStopped.")

        if a.vista:
            while True:
                frame = cam.capturar()
                h, w = frame.shape[:2]
                escala = 960 / w
                cv2.imshow("LACOMER camara (q = salir)", cv2.resize(frame, (960, int(h * escala))))
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            cv2.destroyAllWindows()


if __name__ == "__main__":
    sys.exit(main())
