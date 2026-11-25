"""Keep Kara leases at the boundaries of yt-dlp's own media pipeline."""
from atexit import register
from contextlib import ExitStack
import os
from pathlib import Path
import sqlite3
import sys

from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import PostProcessingError

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from _kara_import_queue import read_transfer_settings
from _kara_import_resources import (AUDIO_PROVIDERS, PROCESSING, YOUTUBE_INTERVAL, audio_slot,
                                   check_youtube, download_scope, resource_slot, wait_service)
from _kara_import_result import ImportIssue


class KaraAdmissionPP(PostProcessor):
    """A YouTube process keeps its provider lease from extraction to body EOF."""

    def __init__(self, downloader, phase):
        super().__init__(downloader)
        self.phase = phase
        self.inbox = Path(os.environ["KARA_YTDLP_INBOX"])
        self.transfers = self.inbox / ".kara-transferts"
        if phase == "transfer":
            # The CLI constructs postprocessors before extracting the video.
            # Preserve the existing provider concurrency across extraction
            # and reception. Only reception competes for the global leases.
            self.network = downloader.kara_network = ExitStack()
            register(self.network.close)
            capacity, max_wait = AUDIO_PROVIDERS["youtube"]
            try:
                self.report("resolution-wait")
                self.provider = self.network.enter_context(resource_slot(self.transfers / "youtube", capacity))
                wait_service("youtube", YOUTUBE_INTERVAL, max_wait=max_wait)
                check_youtube()
                self.report("resolution")
            except (ImportIssue, OSError) as error:
                raise PostProcessingError(str(error)) from error

    def report(self, phase):
        # This protocol includes phase names only. URLs, session cookies and
        # resolved media addresses never cross the progress stream.
        self.to_screen(f"KARA_PHASE {phase}", prefix=False, quiet=False)

    def run(self, info):
        try:
            if self.phase == "transfer":
                self.report("transfer-wait")
                database = Path(os.environ["KARA_YTDLP_QUEUE"])
                identity = os.environ.get("KARA_YTDLP_JOB")
                with download_scope(lambda: read_transfer_settings(database, identity)):
                    # Extraction already paid the local start interval.
                    # Admission still respects cooldowns before opening the body.
                    self.network.enter_context(audio_slot(
                        self.transfers, "youtube", provider_fd=self.provider))
                check_youtube()
                self.report("transfer")
            else:
                # yt-dlp runs this postprocessor before its FFmpeg stages.
                # Release network leases before blocking on CPU admission.
                self._downloader.kara_network.close()
                self.report("conversion-wait")
                processing = ExitStack()
                register(processing.close)
                processing.enter_context(resource_slot(self.inbox / ".kara-traitements", PROCESSING))
                self.report("conversion")
        except (ImportIssue, OSError, sqlite3.Error) as error:
            raise PostProcessingError(str(error)) from error
        return [], info
