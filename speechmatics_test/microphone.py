
from __future__ import annotations
import wave
import threading
from pathlib import Path
import pyaudio

class MicrophoneRecorder:
    RATE = 16000
    CHANNELS = 1
    FORMAT = pyaudio.paInt16
    CHUNK = 3200  # 200ms

    def __init__(self, output: Path):
        self.output=Path(output)
        self.audio=pyaudio.PyAudio()
        self.stream=None
        self.stop_event=threading.Event()
        self.frames=[]

    def __enter__(self):
        self.stream=self.audio.open(
            format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE,
            input=True, frames_per_buffer=self.CHUNK
        )
        return self

    def read(self):
        return self.stream.read(self.CHUNK, exception_on_overflow=False)

    def start(self):
        self.stop_event.clear()

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.output),"wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(self.audio.get_sample_size(self.FORMAT))
            wf.setframerate(self.RATE)
            wf.writeframes(b"".join(self.frames))

    def close(self):
        if self.stream:
            self.stream.stop_stream(); self.stream.close()
        self.audio.terminate()
