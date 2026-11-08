import logging
import queue
import time

from .browser import BrowserError, BrowserTimeout
from .client import validate_config
from .storage import read_json

logger = logging.getLogger(__name__)


class Rotation:
    def __init__(self):
        self.slides = []
        self.index = -1
        self.deadline = 0

    def configure(self, slides):
        if slides != self.slides:
            self.slides = slides
            self.index = -1
            self.deadline = 0

    def due(self, now):
        if not self.slides or now < self.deadline:
            return None
        self.index = (self.index + 1) % len(self.slides)
        slide = self.slides[self.index]
        self.deadline = now + slide["duration"]
        return slide["url"]


def cached_config(state_dir, server):
    cached = read_json(state_dir / "playlist.json")
    if cached is None:
        return None
    if not isinstance(cached, dict) or cached.get("server") != server:
        raise ValueError("Saved playlist belongs to another server. Reset locally first.")
    return validate_config(cached.get("config"))


def run_player(browser, worker, state_dir, server):
    config = cached_config(state_dir, server)
    rotation = Rotation()
    if config:
        rotation.configure(config["slides"])
    worker.start()
    fatal = None
    try:
        while True:
            try:
                event, payload = worker.updates.get_nowait()
                if event == "fatal":
                    config, fatal = None, payload
                    rotation.configure([])
                elif event == "config":
                    config = payload
                    rotation.configure(config["slides"])
            except queue.Empty:
                pass
            try:
                if fatal:
                    browser.status("Player stopped", fatal)
                elif config is None:
                    browser.status("Connecting to PiSign", "Waiting for the server.\nCheck the network connection.")
                elif config["state"] == "unpaired":
                    code = config["code"]
                    browser.status(
                        f"{code[:5]}-{code[5:]}",
                        f"Pair this display at {server}/devices/claim/\n"
                        "Sign in, then enter this code. It expires in 10 minutes.",
                    )
                elif not rotation.slides:
                    browser.status(
                        config.get("name", "Ready to display"),
                        "Your display is paired.\nAdd URLs in your PiSign workspace.",
                    )
                else:
                    url = rotation.due(time.monotonic())
                    if url:
                        browser.navigate(url)
                    try:
                        browser.update_watermark()
                    except BrowserTimeout as exc:
                        logger.warning("%s Skipping this watermark refresh.", exc)
                if browser.process is not None and browser.process.poll() is not None:
                    browser.start()
                    rotation.deadline = 0
            except BrowserError as exc:
                logger.exception("%s Restarting Chromium in five seconds.", exc)
                browser.close()
                rotation.deadline = 0
                time.sleep(5)
            time.sleep(0.1)
    finally:
        worker.stopping.set()
        worker.join(timeout=12)
        browser.close()
