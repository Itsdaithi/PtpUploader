import fnmatch
import logging
import os
import os.path
import re
import shutil

from pathlib import Path

from dynaconf import Dynaconf, Validator


logger = logging.getLogger(__name__)
config = Dynaconf(
    envvar_prefix="PTPUP",
    settings_files=[
        Path(Path(__file__).parent, "config.default.yml"),
        Path("~/.config/ptpuploader/config.yml").expanduser(),
        ".secrets.yaml",
    ],
    environments=False,
    load_dotenv=True,
)
config.validators.register(
    Validator("work_dir", must_exist=True),
    Validator("ptp.username", must_exist=True),
    Validator("ptp.password", must_exist=True),
    Validator("ptp.announce", must_exist=True),
)
config.validators.validate()


class Settings:
    @staticmethod
    def MakeListFromExtensionString(extensions: str):
        # Make sure everything is in lower case in the settings.
        return [i.strip().lower() for i in extensions.split(",")]

    # This makes a list of TagList.
    # Eg.: "A B, C, D E" will become [ [ "A", "B" ], [ "C" ], [ "D", "E" ] ]
    @staticmethod
    def MakeListOfListsFromString(extensions: str):
        return [i.split(" ") for i in Settings.MakeListFromExtensionString(extensions)]

    @staticmethod
    def __HasValidExtensionToUpload(path, extensions):
        tempPath = path.lower()
        for extension in extensions:
            if fnmatch.fnmatch(tempPath, "*." + extension):
                return True

        return False

    @staticmethod
    def HasValidVideoExtensionToUpload(path):
        return Settings.__HasValidExtensionToUpload(
            path, Settings.VideoExtensionsToUpload
        )

    @staticmethod
    def HasValidAdditionalExtensionToUpload(path):
        return Settings.__HasValidExtensionToUpload(
            path, Settings.AdditionalExtensionsToUpload
        )

    @staticmethod
    def IsFileOnIgnoreList(path):
        path = os.path.basename(path)  # We only filter the filenames.
        path = path.lower()
        for ignoreFile in Settings.IgnoreFile:
            if re.match(ignoreFile, path) is not None:
                return True
        return False

    @staticmethod
    def GetAnnouncementWatchPath() -> Path:
        return Path(config.work_dir, "announcement")

    @staticmethod
    def GetAnnouncementInvalidPath() -> Path:
        return Path(config.work_dir, "announcement/invalid")

    @staticmethod
    def GetJobLogPath() -> Path:
        return Path(config.work_dir, "log/job")

    @staticmethod
    def GetTemporaryPath() -> Path:
        return Path(config.work_dir, "temporary")

    @staticmethod
    def GetDatabaseFilePath() -> Path:
        return Path(config.work_dir, "database.sqlite")

    @staticmethod
    def __LoadSceneGroups(path):
        groups = []
        with open(path, "r") as handle:
            for line in handle.readlines():
                groupName = line.strip().lower()
                if groupName:
                    groups.append(groupName)
        return groups

    @staticmethod
    def LoadSettings():
        Settings.VideoExtensionsToUpload = config.uploader.video_files
        Settings.AdditionalExtensionsToUpload = config.uploader.additional_files
        Settings.TorrentClient = None
        Settings.IgnoreFile = config.uploader.ignore_files
        Settings.PtpAnnounceUrl = config.ptp.announce
        Settings.PtpUserName = config.ptp.username
        Settings.PtpPassword = config.ptp.password

        Settings.ImageHost = config.image_host.use
        Settings.PtpImgApiKey = config.image_host.ptpimg.api_key
        Settings.OnSuccessfulUpload = config.hook.on_upload

        Settings.FfmpegPath = config.tools.ffmpeg.path
        Settings.MediaInfoPath = config.tools.mediainfo.path
        Settings.MplayerPath = config.tools.mplayer.path
        Settings.MpvPath = config.tools.mpv.path
        Settings.UnrarPath = config.tools.unrar.path
        Settings.ImageMagickConvertPath = config.tools.imagemagick.path

        Settings.WorkingPath = config.work_dir

        Settings.AllowReleaseTag = Settings.MakeListOfListsFromString(
            config.source._default.allow_tags
        )
        Settings.IgnoreReleaseTag = Settings.MakeListOfListsFromString(
            config.source._default.ignore_tags
        )
        Settings.IgnoreReleaserGroup = config.source._default.ignore_release_group

        scene_file = Path(
            os.path.expanduser("~/.config/ptpuploader"), "scene_groups.txt"
        )
        if not scene_file.exists():
            scene_file = Path(Path(__name__).parent, "SceneGroups.txt")
        Settings.SceneReleaserGroup = Settings.__LoadSceneGroups(scene_file)

        Settings.GreasemonkeyTorrentSenderPassword = config.web.api_key
        Settings.OverrideScreenshots = config.uploader.override_screenshots
        Settings.ForceDirectorylessSingleFileTorrent = (
            config.uploader.force_directoryless_single_file
        )
        Settings.PersonalRip = config.uploader.is_personal
        Settings.ReleaseNotes = config.uploader.release_notes
        Settings.SkipDuplicateChecking = config.uploader.skip_duplicate_checking

        Settings.SizeLimitForAutoCreatedJobs = (
            float(config.source._default.max_size) * 1024 * 1024 * 1024
        )
        Settings.StopIfSynopsisIsMissing = (
            config.source._default.stop_if_synopsis_missing
        )
        Settings.StopIfCoverArtIsMissing = config.source._default.stop_if_art_missing
        Settings.StopIfImdbRatingIsLessThan = config.source._default.min_imdb_rating
        Settings.StopIfImdbVoteCountIsLessThan = config.source._default.min_imdb_votes
        Settings.MediaInfoTimeOut = config.tools.mediainfo.timeout

        Settings.TorrentClientName = config.client.use
        Settings.TorrentClientAddress = config["client"][config.client.use]["address"]

        # Create required directories.
        Settings.GetAnnouncementInvalidPath().mkdir(parents=True, exist_ok=True)
        Settings.GetJobLogPath().mkdir(parents=True, exist_ok=True)
        Settings.GetTemporaryPath().mkdir(parents=True, exist_ok=True)

    @staticmethod
    def GetTorrentClient():
        if Settings.TorrentClient is None:
            if Settings.TorrentClientName.lower() == "transmission":
                from PtpUploader.Tool.Transmission import Transmission

                Settings.TorrentClient = Transmission(
                    Settings.TorrentClientAddress.split(":")[0],
                    Settings.TorrentClientAddress.split(":")[1],
                )
            else:
                from PtpUploader.Tool.Rtorrent import Rtorrent

                Settings.TorrentClient = Rtorrent()
        return Settings.TorrentClient

    @staticmethod
    def VerifyPaths():
        logger.info("Checking paths")

        if shutil.which(Settings.MediaInfoPath) is None:
            logger.critical(
                "Mediainfo not found with command '%s'!", Settings.MediaInfoPath
            )
            return False

        if (
            shutil.which(Settings.MpvPath) is None
            and shutil.which(Settings.MplayerPath) is None
            and shutil.which(Settings.FfmpegPath) is None
        ):
            logger.critical("At least one of mpv, mplayer or ffmpeg is required!")
            return False

        # Optional
        if Settings.UnrarPath and not shutil.which(Settings.UnrarPath):
            logger.error("Unrar path is set but not found: %s", Settings.UnrarPath)
        if Settings.ImageMagickConvertPath and not shutil.which(
            Settings.ImageMagickConvertPath
        ):
            logger.error(
                "ImageMagick path is set but not found: %s",
                Settings.ImageMagickConvertPath,
            )

        return True
