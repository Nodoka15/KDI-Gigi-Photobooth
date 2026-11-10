import logging
import math
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageOps, ImageTk

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
CAPTURES_DIR = APP_DIR / "captures"
PRINTER_DEV = "/dev/usb/lp0"
PRINT_WIDTH = 384
PRINT_SIDE_PADDING = 12
COUNTDOWN_SECONDS = 10
SUMMARY_IDLE_SECONDS = 30

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_camera() -> Any:
    """Create and start the camera used for both live preview and stills."""
    from picamera2 import Picamera2

    camera = Picamera2()
    camera.configure(
        camera.create_still_configuration(
            main={"size": (1920, 1440), "format": "RGB888"}
        )
    )
    camera.start()
    return camera


def compose_print_image(photo_paths: list[Path]) -> Image.Image:
    """Build the one- or two-photo strip with the club branding."""
    if len(photo_paths) not in (1, 2):
        raise ValueError("A print must contain one or two photos.")

    edge_padding = PRINT_SIDE_PADDING
    photo_width = PRINT_WIDTH - edge_padding * 2
    photos: list[Image.Image] = []
    for path in photo_paths:
        with Image.open(path) as source:
            photo = source.convert("RGB")
        height = round(photo_width * photo.height / photo.width)
        photos.append(photo.resize((photo_width, height), Image.Resampling.LANCZOS))

    mascot_gap = 112 if len(photos) == 2 else 0
    footer_height = 164
    content_height = sum(photo.height for photo in photos) + mascot_gap
    output = Image.new(
        "RGB",
        (PRINT_WIDTH, edge_padding * 2 + content_height + footer_height),
        "white",
    )

    y = edge_padding
    output.paste(photos[0], (edge_padding, y))
    y += photos[0].height

    if len(photos) == 2:
        mascot_path = ASSETS_DIR / "GigiFace.png"
        with Image.open(mascot_path) as source:
            mascot = source.convert("RGBA")
        mascot.thumbnail((88, 88), Image.Resampling.LANCZOS)
        mascot_x = (PRINT_WIDTH - mascot.width) // 2
        mascot_y = y + (mascot_gap - mascot.height) // 2
        output.paste(mascot, (mascot_x, mascot_y), mascot)
        y += mascot_gap
        output.paste(photos[1], (edge_padding, y))

    footer_y = edge_padding + content_height
    qr_width = 96
    qr_x = PRINT_WIDTH - edge_padding - qr_width
    logo_region_width = qr_x - edge_padding * 2
    logo_path = ASSETS_DIR / "KDILogo.png"
    with Image.open(logo_path) as source:
        logo = source.convert("RGBA")
    logo_bounds = logo.getchannel("A").getbbox()
    if logo_bounds is None:
        raise ValueError(f"Logo asset has no visible pixels: {logo_path}")
    logo = logo.crop(logo_bounds)
    logo_height = 72
    logo_width = round(logo.width * logo_height / logo.height)
    if logo_width > logo_region_width:
        logo_height = round(logo_height * logo_region_width / logo_width)
        logo_width = logo_region_width
    logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)
    qr_y = footer_y + footer_height - qr_width - edge_padding
    output.paste(
        logo,
        (
            (PRINT_WIDTH - logo.width) // 2,
            qr_y + (qr_width - logo.height) // 2,
        ),
        logo,
    )

    qr_path = ASSETS_DIR / "kdiqrcode.png"
    with Image.open(qr_path) as source:
        qr_code = source.convert("RGBA")
    qr_code = qr_code.resize(
        (qr_width, qr_width),
        Image.Resampling.NEAREST,
    )
    qr_luminance = qr_code.convert("L").point(lambda pixel: 0 if pixel < 128 else 255)
    qr_code = Image.merge(
        "RGBA",
        (qr_luminance, qr_luminance, qr_luminance, qr_code.getchannel("A")),
    )
    output.paste(
        qr_code,
        (
            PRINT_WIDTH - edge_padding - qr_code.width,
            qr_y,
        ),
        qr_code,
    )
    return output


def prepare_thermal_bitmap(image: Image.Image) -> Image.Image:
    """Resize, enhance, and dither a PIL image for the thermal printer."""
    img = image.convert("L")
    target_height = int(PRINT_WIDTH * img.height / img.width)
    img = img.resize((PRINT_WIDTH, target_height), Image.Resampling.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=2)
    img = ImageEnhance.Brightness(img).enhance(1.15)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    return ImageOps.invert(img).convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def prepare_thermal_image(image_path: str | Path) -> Image.Image:
    """Load, resize, enhance, and dither an image file for the printer."""
    with Image.open(image_path) as source:
        return prepare_thermal_bitmap(source)


def print_image_to_thermal(
    img_1bit: Image.Image,
    device: str | Path = PRINTER_DEV,
) -> None:
    """Stream a 1-bit image to the printer using GS v 0 raster commands."""
    width_bytes = img_1bit.width // 8
    total_height = img_1bit.height
    raw_bytes = img_1bit.tobytes()

    with open(device, "wb") as printer:
        printer.write(b"\x1b\x40\x1b\x37\x02\x5a\x04")
        printer.write(b"\x1b\x61\x01")

        chunk_rows = 64
        for y in range(0, total_height, chunk_rows):
            rows = min(chunk_rows, total_height - y)
            chunk_data = raw_bytes[y * width_bytes : (y + rows) * width_bytes]

            x_low = width_bytes & 0xFF
            x_high = (width_bytes >> 8) & 0xFF
            y_low = rows & 0xFF
            y_high = (rows >> 8) & 0xFF
            header = bytes(
                [0x1D, 0x76, 0x30, 0x00, x_low, x_high, y_low, y_high]
            )
            printer.write(header + chunk_data)
            printer.flush()
            time.sleep(0.05)

        printer.write(b"\n\x1b\x64\x04")
        printer.flush()


class PhotoBoothApp:
    def __init__(self, root: tk.Tk) -> None:
        from gpiozero import DigitalOutputDevice, PWMLED

        self.root = root
        self.led1 = PWMLED(17)
        self.led2 = PWMLED(5)
        self.buzzer = DigitalOutputDevice(26, active_high=True, initial_value=False)
        self.canvas = tk.Canvas(root, highlightthickness=0, bg="#fffaf2")
        self.canvas.pack(fill="both", expand=True)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.image_refs: list[ImageTk.PhotoImage] = []
        self.state = "welcome"
        self.selected_count = 1
        self.photos: list[Path] = []
        self.session_stamp = ""
        self.countdown_deadline = 0.0
        self.led_is_on = False
        self.camera: Any | None = None
        self.worker_poll_id: str | None = None
        self.countdown_id: str | None = None
        self.preview_id: str | None = None
        self.welcome_animation_id: str | None = None
        self.summary_idle_id: str | None = None
        self.welcome_frames: list[tuple[Image.Image, int]] = []
        self.welcome_frame_index = 0
        self.preview_stop_event = threading.Event()
        self.preview_thread: threading.Thread | None = None
        self.preview_frames: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.latest_preview: Image.Image | None = None

        self.summary_background = self._load_asset("background.png")
        self.menu_background = self.summary_background
        self._load_welcome_animation("GigiFaceAnimation2.gif")
        self.root.title("Gigi's Photo Booth")
        self.root.attributes("-fullscreen", True)
        self.root.bind("<Escape>", self.close)
        self.canvas.bind("<Configure>", self._render)
        self._render()
        self._schedule_welcome_animation()
        self.worker_poll_id = self.root.after(100, self._process_worker_events)

    def _load_asset(self, filename: str) -> Image.Image:
        path = ASSETS_DIR / filename
        if not path.is_file():
            raise FileNotFoundError(f"Required image asset is missing: {path}")
        with Image.open(path) as source:
            return source.convert("RGBA")

    def _load_welcome_animation(self, filename: str) -> None:
        path = ASSETS_DIR / filename
        if not path.is_file():
            raise FileNotFoundError(f"Required animation asset is missing: {path}")

        with Image.open(path) as animation:
            for frame_index in range(getattr(animation, "n_frames", 1)):
                animation.seek(frame_index)
                frame = animation.convert("RGBA").copy()
                duration = max(20, animation.info.get("duration", 100))
                self.welcome_frames.append((frame, duration))

        if not self.welcome_frames:
            raise ValueError(f"Animation contains no frames: {path}")

    def _schedule_welcome_animation(self) -> None:
        if self.state != "welcome":
            return
        _, duration = self.welcome_frames[self.welcome_frame_index]
        self.welcome_animation_id = self.root.after(
            duration,
            self._advance_welcome_animation,
        )

    def _advance_welcome_animation(self) -> None:
        if self.state != "welcome":
            return
        self.welcome_frame_index = (
            self.welcome_frame_index + 1
        ) % len(self.welcome_frames)
        self._render()
        self._schedule_welcome_animation()

    def _handle_screen_tap(self, _event: tk.Event) -> str:
        if self.state == "welcome":
            self._cancel_callback("welcome_animation_id")
            self.state = "menu"
            self._render()
        return "break"

    def _return_to_welcome(self) -> None:
        self._cancel_callback("summary_idle_id")
        self.welcome_frame_index = 0
        self.state = "welcome"
        self._render()
        self._schedule_welcome_animation()

    def _cancel_callback(self, attribute: str) -> None:
        callback_id = getattr(self, attribute)
        if callback_id is not None:
            try:
                self.root.after_cancel(callback_id)
            except tk.TclError:
                logger.debug("Tk callback %s was already cancelled", callback_id)
            setattr(self, attribute, None)

    def _show_image(
        self,
        image: Image.Image,
        x: float,
        y: float,
        width: int,
        height: int,
        *,
        cover: bool = False,
        corner_radius: int = 0,
        tags: tuple[str, ...] = (),
    ) -> None:
        width = max(width, 1)
        height = max(height, 1)
        if cover:
            resized = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
        else:
            resized = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
        if corner_radius:
            mask = Image.new("L", resized.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, resized.width - 1, resized.height - 1),
                radius=corner_radius,
                fill=255,
            )
            resized.putalpha(mask)
        photo = ImageTk.PhotoImage(resized)
        self.image_refs.append(photo)
        self.canvas.create_image(x, y, image=photo, anchor="center", tags=tags)

    def _render(self, _event: tk.Event | None = None) -> None:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return

        self.canvas.delete("all")
        self.image_refs.clear()

        if self.state == "welcome":
            self._render_welcome(width, height)
        elif self.state == "menu":
            self._render_menu(width, height)
        elif self.state in ("starting", "countdown", "capturing", "printing"):
            self._render_progress(width, height)
        elif self.state == "summary":
            self._render_summary(width, height)
        elif self.state == "error":
            self._render_error(width, height)

    def _render_welcome(self, width: int, height: int) -> None:
        frame, _duration = self.welcome_frames[self.welcome_frame_index]
        self._show_image(
            frame,
            width / 2,
            height / 2,
            width,
            height,
            cover=True,
            tags=("welcome-screen",),
        )
        self.canvas.tag_bind(
            "welcome-screen",
            "<Button-1>",
            self._handle_screen_tap,
        )

    def _render_menu(self, width: int, height: int) -> None:
        self._show_image(
            self.menu_background,
            width / 2,
            height / 2,
            width,
            height,
            cover=True,
        )
        self.canvas.create_text(
            width / 2,
            height * 0.21,
            text="Choose your photo strip",
            font=("Helvetica", max(22, int(height * 0.05)), "bold"),
            fill="#171717",
        )
        button_width = min(width * 0.34, 390)
        button_height = min(height * 0.19, 150)
        self._draw_button(
            width * 0.31,
            height * 0.55,
            "1 PHOTO",
            "one-photo",
            int(button_width),
            int(button_height),
        )
        self._draw_button(
            width * 0.69,
            height * 0.55,
            "2 PHOTOS",
            "two-photos",
            int(button_width),
            int(button_height),
        )

    def _render_progress(self, width: int, height: int) -> None:
        self._show_image(
            self.summary_background,
            width / 2,
            height / 2,
            width,
            height,
            cover=True,
        )
        if self.state == "starting":
            text = "Starting\ncamera"
            detail = "Please wait"
        elif self.state == "countdown":
            remaining = max(0, math.ceil(self.countdown_deadline - time.monotonic()))
            text = str(remaining)
            detail = f"Photo {len(self.photos) + 1} of {self.selected_count}"
        elif self.state == "capturing":
            text = "Smile"
            detail = f"Taking photo {len(self.photos) + 1} of {self.selected_count}"
        else:
            text = "Printing"
            detail = "Your Gigi photo strip is on its way"

        if self.state == "countdown" and self.latest_preview is not None:
            preview_width = min(int(width * 0.64), 720)
            preview_height = min(int(height * 0.57), 540)
            self._show_image(
                self.latest_preview,
                width * 0.48,
                height * 0.49,
                preview_width,
                preview_height,
                corner_radius=max(12, min(32, preview_width // 20)),
            )
            card_width = min(width * 0.19, 230)
            card_height = min(height * 0.24, 180)
            self._draw_rounded_rectangle(
                width * 0.82 - card_width / 2,
                height * 0.33 - card_height / 2,
                width * 0.82 + card_width / 2,
                height * 0.33 + card_height / 2,
                fill="#ffffff",
                outline="#ffffff",
                radius=min(30, int(card_height * 0.15)),
            )
            self.canvas.create_text(
                width * 0.82,
                height * 0.33,
                text=text,
                font=("Helvetica", max(38, int(height * 0.13)), "bold"),
                fill="#34772d",
            )
            self.canvas.create_text(
                width / 2,
                height * 0.84,
                text=detail,
                font=("Helvetica", max(15, int(height * 0.032))),
                fill="#171717",
            )
            return

        card_width = min(width * 0.72, 620)
        card_height = height * 0.56
        self._draw_rounded_rectangle(
            width / 2 - card_width / 2,
            height * 0.48 - card_height / 2,
            width / 2 + card_width / 2,
            height * 0.48 + card_height / 2,
            fill="#ffffff",
            outline="#ffffff",
            radius=min(36, int(card_height * 0.12)),
        )
        self.canvas.create_text(
            width / 2,
            height * (0.46 if self.state == "starting" else 0.43),
            text=text,
            font=(
                "Helvetica",
                max(28, min(48, int(height * 0.075)))
                if self.state == "starting"
                else max(44, int(height * 0.15)),
                "bold",
            ),
            fill="#34772d",
        )
        self.canvas.create_text(
            width / 2,
            height * 0.64,
            text=detail,
            font=("Helvetica", max(15, int(height * 0.032))),
            fill="#171717",
        )

    def _render_summary(self, width: int, height: int) -> None:
        self._show_image(
            self.summary_background,
            width / 2,
            height / 2,
            width,
            height,
            cover=True,
        )
        self.canvas.create_text(
            width / 2,
            height * 0.11,
            text="Your photos are ready",
            font=("Helvetica", max(20, int(height * 0.05)), "bold"),
            fill="#171717",
        )

        preview_height = min(int(height * 0.40), 280)
        preview_width = min(
            int(width * (0.34 if len(self.photos) == 2 else 0.48)),
            int(preview_height * 1.34),
        )
        centers = (
            [width * 0.36, width * 0.64]
            if len(self.photos) == 2
            else [width / 2]
        )
        for path, center_x in zip(self.photos, centers):
            with Image.open(path) as source:
                preview = source.convert("RGB")
            self._show_image(
                preview,
                center_x,
                height * 0.47,
                preview_width,
                preview_height,
                corner_radius=max(8, min(24, preview_width // 16)),
            )

        self._draw_button(
            width * 0.36,
            height * 0.83,
            "TAKE ANOTHER PHOTO",
            "again",
            max(220, int(width * 0.31)),
            int(height * 0.10),
        )
        self._draw_button(
            width * 0.70,
            height * 0.83,
            "START OVER",
            "home",
            max(170, int(width * 0.22)),
            int(height * 0.10),
            fill="#ffffff",
            text_fill="#34772d",
        )

    def _render_error(self, width: int, height: int) -> None:
        self.canvas.create_text(
            width / 2,
            height * 0.25,
            text="We couldn't finish your photo",
            font=("Helvetica", max(22, int(height * 0.05)), "bold"),
            fill="#9d2c24",
        )
        self.canvas.create_text(
            width / 2,
            height * 0.47,
            text=self.error_message,
            width=width * 0.8,
            font=("Helvetica", max(12, int(height * 0.022))),
            fill="#171717",
        )
        self._draw_button(
            width * 0.36,
            height * 0.75,
            "TRY AGAIN",
            "retry",
            max(190, int(width * 0.28)),
            int(height * 0.10),
        )
        self._draw_button(
            width * 0.70,
            height * 0.75,
            "START OVER",
            "home",
            max(170, int(width * 0.22)),
            int(height * 0.10),
            fill="#ffffff",
            text_fill="#34772d",
        )

    def _draw_button(
        self,
        x: float,
        y: float,
        text: str,
        action: str,
        width: int,
        height: int,
        *,
        fill: str = "#579f42",
        text_fill: str = "white",
    ) -> None:
        tag = f"button-{action}"
        self._draw_rounded_rectangle(
            x - width / 2,
            y - height / 2,
            x + width / 2,
            y + height / 2,
            fill=fill,
            outline="#34772d",
            width=3,
            radius=min(24, height // 3),
            tags=(tag,),
        )
        self.canvas.create_text(
            x,
            y,
            text=text,
            font=("Helvetica", max(11, height // 5), "bold"),
            fill=text_fill,
            tags=(tag,),
        )
        self.canvas.tag_bind(
            tag,
            "<Button-1>",
            lambda _event, selected=action: self._handle_button(selected),
        )

    def _draw_rounded_rectangle(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        fill: str,
        outline: str,
        width: int = 1,
        radius: int = 20,
        tags: tuple[str, ...] = (),
    ) -> None:
        radius = min(radius, int((x2 - x1) / 2), int((y2 - y1) / 2))
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=12,
            fill=fill,
            outline=outline,
            width=width,
            tags=tags,
        )

    def _handle_button(self, action: str) -> str:
        if action == "one-photo":
            self._start_session(1)
        elif action == "two-photos":
            self._start_session(2)
        elif action == "home":
            self._return_to_welcome()
        elif action == "again":
            self._start_session(self.selected_count)
        elif action == "retry":
            self._start_session(self.selected_count)
        return "break"

    def _show_summary(self) -> None:
        self.state = "summary"
        self._render()
        self._cancel_callback("summary_idle_id")
        self.summary_idle_id = self.root.after(
            SUMMARY_IDLE_SECONDS * 1000,
            self._return_to_welcome,
        )

    def _start_session(self, photo_count: int) -> None:
        self.selected_count = photo_count
        self.photos = []
        self.session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        self.latest_preview = None
        self.state = "starting"
        self._render()
        self._run_worker("camera-start", Path())

    def _begin_countdown(self) -> None:
        self.state = "countdown"
        self.countdown_deadline = time.monotonic() + COUNTDOWN_SECONDS
        self.led_is_on = False
        self.led1.off()
        self.led2.off()
        self._schedule_preview_update()
        self._countdown_tick()

    def _start_preview_worker(self) -> None:
        if self.camera is None:
            raise RuntimeError("Cannot start preview before the camera is ready.")
        camera = self.camera
        self.preview_stop_event.clear()

        def capture_frames() -> None:
            while not self.preview_stop_event.is_set():
                try:
                    frame = camera.capture_array()
                    try:
                        self.preview_frames.put_nowait(frame)
                    except queue.Full:
                        try:
                            self.preview_frames.get_nowait()
                        except queue.Empty:
                            pass
                        self.preview_frames.put_nowait(frame)
                    time.sleep(0.03)
                except Exception as exc:
                    if not self.preview_stop_event.is_set():
                        logger.exception("Camera preview failed")
                        self.events.put(("preview-error", str(exc)))
                    return

        self.preview_thread = threading.Thread(target=capture_frames, daemon=True)
        self.preview_thread.start()

    def _stop_preview_worker(self) -> None:
        self.preview_stop_event.set()
        if self.preview_thread is not None:
            self.preview_thread.join(timeout=2)
            if self.preview_thread.is_alive():
                raise TimeoutError("Camera preview did not stop in time.")
            self.preview_thread = None

    def _schedule_preview_update(self) -> None:
        if self.state == "countdown":
            self.preview_id = self.root.after(100, self._update_live_preview)

    def _update_live_preview(self) -> None:
        if self.state != "countdown":
            return
        updated = False
        while True:
            try:
                frame = self.preview_frames.get_nowait()
            except queue.Empty:
                break
            self.latest_preview = Image.fromarray(frame[:, :, ::-1].copy()).convert("RGB")
            updated = True
        if updated:
            self._render()
        self._schedule_preview_update()

    def _countdown_tick(self) -> None:
        if self.state != "countdown":
            return

        remaining = max(0, math.ceil(self.countdown_deadline - time.monotonic()))
        if remaining == 0:
            self._cancel_callback("preview_id")
            self.led1.off()
            self.led2.off()
            self.state = "capturing"
            self._render()
            photo_index = len(self.photos) + 1
            photo_path = CAPTURES_DIR / f"photo_{self.session_stamp}_{photo_index}.jpg"
            self._run_worker("capture", photo_path)
            return

        self.led_is_on = not self.led_is_on
        self.led1.value = float(self.led_is_on)
        self.led2.value = float(self.led_is_on)
        self._render()
        interval = 250 if remaining <= 3 else 1000
        self.countdown_id = self.root.after(interval, self._countdown_tick)

    def _run_worker(self, operation: str, payload: Path | list[Path]) -> None:
        def work() -> None:
            try:
                if operation == "camera-start":
                    camera = create_camera()
                    self.events.put(("camera-ready", camera))
                elif operation == "capture":
                    if isinstance(payload, list):
                        raise TypeError("Capture worker requires one output path.")
                    self._stop_preview_worker()
                    if self.camera is None:
                        raise RuntimeError("Camera is not available.")
                    path = Path(payload)
                    self.buzzer.blink(
                        on_time=0.15,
                        off_time=0,
                        n=1,
                        background=True,
                    )
                    self.camera.capture_file(str(path))
                    self.events.put(("photo-captured", str(path)))
                elif operation == "print":
                    if not isinstance(payload, list):
                        raise TypeError("Print worker requires a list of photo paths.")
                    image = compose_print_image(payload)
                    print_image_to_thermal(prepare_thermal_bitmap(image))
                    self._close_camera_resource()
                    self.events.put(("printed", ""))
                elif operation == "camera-stop":
                    self._close_camera_resource()
                    self.events.put(("camera-stopped", ""))
                else:
                    raise ValueError(f"Unknown photo booth operation: {operation}")
            except Exception as exc:
                logger.exception("Photo booth %s failed", operation)
                self.buzzer.off()
                try:
                    self._close_camera_resource()
                except Exception:
                    logger.exception("Could not close the camera after an error")
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _close_camera_resource(self) -> None:
        self._stop_preview_worker()
        if self.camera is not None:
            try:
                self.camera.stop()
            finally:
                self.camera.close()

    def _process_worker_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "camera-ready":
                    self.camera = payload
                    self._start_preview_worker()
                    self._begin_countdown()
                elif event == "camera-stopped":
                    self.camera = None
                elif event == "photo-captured":
                    self.photos.append(Path(str(payload)))
                    if len(self.photos) < self.selected_count:
                        self.latest_preview = None
                        self._start_preview_worker()
                        self._begin_countdown()
                    else:
                        self.state = "printing"
                        self._render()
                        self._run_worker("print", self.photos.copy())
                elif event == "printed":
                    self.camera = None
                    self._show_summary()
                elif event == "preview-error":
                    self.error_message = str(payload)
                    self.state = "error"
                    self._cancel_callback("preview_id")
                    self.led1.off()
                    self.led2.off()
                    self._render()
                    self._run_worker("camera-stop", Path())
                else:
                    self.error_message = str(payload)
                    self.state = "error"
                    self.camera = None
                    self._cancel_callback("preview_id")
                    self.led1.off()
                    self.led2.off()
                    self._render()
        except queue.Empty:
            pass
        self.worker_poll_id = self.root.after(100, self._process_worker_events)

    def close(self, _event: tk.Event | None = None) -> None:
        for attribute in (
            "countdown_id",
            "worker_poll_id",
            "preview_id",
            "welcome_animation_id",
            "summary_idle_id",
        ):
            self._cancel_callback(attribute)
        self.preview_stop_event.set()
        if self.camera is not None:
            try:
                self._close_camera_resource()
            except Exception:
                logger.exception("Could not close camera during shutdown")
        self.led1.close()
        self.led2.close()
        self.buzzer.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        PhotoBoothApp(root)
    except Exception as exc:
        logger.exception("Could not start the photo booth")
        messagebox.showerror("Photo booth startup failed", str(exc), parent=root)
        root.destroy()
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
