import datetime
import logging
import os
import subprocess
import threading

from PtpUploader import Ptp
from PtpUploader.Helper import GetIdxSubtitleLanguages, TimeDifferenceToText
from PtpUploader.ImageHost.ImageUploader import ImageUploader
from PtpUploader.Job.FinishedJobPhase import FinishedJobPhase
from PtpUploader.Job.JobRunningState import JobRunningState
from PtpUploader.Job.WorkerBase import WorkerBase
from PtpUploader.MyGlobals import MyGlobals
from PtpUploader.PtpSubtitle import PtpSubtitleId
from PtpUploader.PtpUploaderException import *
from PtpUploader.ReleaseDescriptionFormatter import ReleaseDescriptionFormatter
from PtpUploader.ReleaseInfo import ReleaseInfo
from PtpUploader.Settings import Settings
from PtpUploader.Tool import Mktor


logger = logging.getLogger(__name__)


class Upload(WorkerBase):
    def __init__(self, release_id: int, stop_requested: threading.Event):
        super().__init__(release_id, stop_requested)
        self.Phases = [
            self.__SetInProgress,
            self.__StopAutomaticJobBeforeExtracting,
            self.__StopAutomaticJobIfThereAreMultipleVideosBeforeExtracting,
            self.__CreateUploadPath,
            self.__MakeIncludedFileList,
            self.__ExtractRelease,
            self.__ValidateExtractedRelease,
            self.__MakeReleaseDescription,
            self.__DetectSubtitles,
            self.__MakeTorrent,
            self.__CheckIfExistsOnPtp,
            self.__CheckSynopsis,
            self.__CheckCoverArt,
            self.__RehostPoster,
            self.__StopBeforeUploading,
            self.__StartTorrent,
            self.__UploadMovie,
            self.__ExecuteCommandOnSuccessfulUpload,
        ]

        self.TorrentClient = Settings.GetTorrentClient()
        self.IncludedFileList = None
        self.VideoFiles = []
        self.AdditionalFiles = []
        self.MainMediaInfo = None
        self.ReleaseDescription = ""

    def __SetInProgress(self):
        self.ReleaseInfo.JobRunningState = JobRunningState.InProgress
        self.ReleaseInfo.save()

    def __StopAutomaticJobBeforeExtracting(self):
        if (
            self.ReleaseInfo.IsUserCreatedJob()
            or self.ReleaseInfo.AnnouncementSource.StopAutomaticJob
            != "beforeextracting"
        ):
            return

        raise PtpUploaderException("Stopping before extracting.")

    def __StopAutomaticJobIfThereAreMultipleVideosBeforeExtracting(self):
        if (
            self.ReleaseInfo.IsUserCreatedJob()
            or self.ReleaseInfo.AnnouncementSource.StopAutomaticJobIfThereAreMultipleVideos
            != "beforeextracting"
        ):
            return

        includedFileList = self.ReleaseInfo.AnnouncementSource.GetIncludedFileList(
            self.ReleaseInfo
        )
        self.ReleaseInfo.AnnouncementSource.CheckFileList(
            self.ReleaseInfo, includedFileList
        )

    def __CreateUploadPath(self):
        if self.ReleaseInfo.IsJobPhaseFinished(
            FinishedJobPhase.Upload_CreateUploadPath
        ):
            self.logger.info(
                "Upload path creation phase has been reached previously, not creating it again."
            )
            return

        customUploadPath = self.ReleaseInfo.AnnouncementSource.GetCustomUploadPath(
            self.logger, self.ReleaseInfo
        )
        if len(customUploadPath) > 0:
            self.ReleaseInfo.ReleaseUploadPath = customUploadPath

        self.ReleaseInfo.AnnouncementSource.CreateUploadDirectory(self.ReleaseInfo)

        self.ReleaseInfo.SetJobPhaseFinished(FinishedJobPhase.Upload_CreateUploadPath)
        self.ReleaseInfo.save()

    def __MakeIncludedFileList(self):
        self.IncludedFileList = self.ReleaseInfo.AnnouncementSource.GetIncludedFileList(
            self.ReleaseInfo
        )

        if len(self.ReleaseInfo.IncludedFiles) > 0:
            self.logger.info(
                "There are %s files in the file list. Customized: '%s'.",
                len(self.IncludedFileList.Files),
                self.ReleaseInfo.IncludedFiles,
            )
        else:
            self.logger.info(
                "There are %s files in the file list.", len(self.IncludedFileList.Files)
            )

        self.IncludedFileList.ApplyCustomizationFromJson(self.ReleaseInfo.IncludedFiles)

    def __ExtractRelease(self):
        if self.ReleaseInfo.IsJobPhaseFinished(FinishedJobPhase.Upload_ExtractRelease):
            self.logger.info(
                "Extract release phase has been reached previously, not extracting release again."
            )
            return

        self.ReleaseInfo.AnnouncementSource.ExtractRelease(
            self.logger, self.ReleaseInfo, self.IncludedFileList
        )

        self.ReleaseInfo.SetJobPhaseFinished(FinishedJobPhase.Upload_ExtractRelease)
        self.ReleaseInfo.save()

    def __ValidateExtractedRelease(self):
        (
            self.VideoFiles,
            self.AdditionalFiles,
        ) = self.ReleaseInfo.AnnouncementSource.ValidateExtractedRelease(
            self.ReleaseInfo, self.IncludedFileList
        )

    def __GetMediaInfoContainer(self, mediaInfo):
        container = ""

        if mediaInfo.IsAvi():
            container = "AVI"
        elif mediaInfo.IsMkv():
            container = "MKV"
        elif mediaInfo.IsMp4():
            container = "MP4"
            if not self.ReleaseInfo == ReleaseInfo.SourceChoices.HDTV:
                raise PtpUploaderException("MP4 only allowed for HDTV sources")
        elif mediaInfo.IsVob():
            container = "VOB IFO"

        if self.ReleaseInfo.Container:
            if container != self.ReleaseInfo.Container:
                if self.ReleaseInfo.IsForceUpload():
                    self.logger.info(
                        "Container is set to '%s', detected MediaInfo container is '%s' ('%s'). Ignoring mismatch because of force upload.",
                        self.ReleaseInfo.Container,
                        container,
                        mediaInfo.Container,
                    )
                else:
                    raise PtpUploaderException(
                        "Container is set to '%s', detected MediaInfo container is '%s' ('%s')."
                        % (self.ReleaseInfo.Container, container, mediaInfo.Container)
                    )
        elif len(container) > 0:
            self.ReleaseInfo.Container = container
        else:
            raise PtpUploaderException(
                "Unsupported container: '%s'." % mediaInfo.Container
            )

    @staticmethod
    def __CanIgnoreDetectedAndSetCodecDifference(detected, set):
        return (detected == "x264" and set == "H.264") or (
            detected == "H.264" and set == "x264"
        )

    def __GetMediaInfoCodec(self, mediaInfo):
        codec = ""

        if mediaInfo.IsX264():
            codec = "x264"
            if mediaInfo.IsAvi():
                raise PtpUploaderException("X264 in AVI is not allowed.")
        elif mediaInfo.IsH264():
            codec = "H.264"
            if mediaInfo.IsAvi():
                raise PtpUploaderException("H.264 in AVI is not allowed.")
        elif mediaInfo.IsXvid():
            codec = "XviD"
            if mediaInfo.IsMkv():
                raise PtpUploaderException("XviD in MKV is not allowed.")
        elif mediaInfo.IsDivx():
            codec = "DivX"
            if mediaInfo.IsMkv():
                raise PtpUploaderException("DivX in MKV is not allowed.")
        elif mediaInfo.IsVc1():
            codec = "VC-1"
        elif mediaInfo.IsMpeg2Codec():
            codec = "MPEG-2"
        elif self.ReleaseInfo.IsDvdImage():
            # Codec type DVD5 and DVD9 can't be figured out from MediaInfo.
            codec = self.ReleaseInfo.Codec

        if self.ReleaseInfo.Codec:
            if codec != self.ReleaseInfo.Codec:
                if Upload.__CanIgnoreDetectedAndSetCodecDifference(
                    codec, self.ReleaseInfo.Codec
                ):
                    if "Remux" not in self.ReleaseInfo.RemasterTitle:
                        self.logger.info(
                            "Codec is set to '%s', detected MediaInfo codec is '%s' ('%s'). Using the detected codec.",
                            self.ReleaseInfo.Codec,
                            codec,
                            mediaInfo.Codec,
                        )
                        self.ReleaseInfo.Codec = codec
                elif self.ReleaseInfo.IsForceUpload():
                    self.logger.info(
                        "Codec is set to '%s', detected MediaInfo codec is '%s' ('%s'). Ignoring mismatch because of force upload.",
                        self.ReleaseInfo.Codec,
                        codec,
                        mediaInfo.Codec,
                    )
                else:
                    raise PtpUploaderException(
                        "Codec is set to '%s', detected MediaInfo codec is '%s' ('%s')."
                        % (self.ReleaseInfo.Codec, codec, mediaInfo.Codec)
                    )
        elif len(codec) > 0:
            self.ReleaseInfo.Codec = codec
        else:
            raise PtpUploaderException("Unsupported codec: '%s'." % mediaInfo.Codec)

    def __GetMediaInfoResolution(self, mediaInfo):
        resolution = ""

        # Indicate the exact resolution for standard definition releases.
        # It is not needed for DVD images.
        if self.ReleaseInfo.IsStandardDefinition() and (
            not self.ReleaseInfo.IsDvdImage()
        ):
            resolution = "%sx%s" % (mediaInfo.Width, mediaInfo.Height)

        if len(self.ReleaseInfo.Resolution) > 0:
            if resolution != self.ReleaseInfo.Resolution:
                if self.ReleaseInfo.IsForceUpload():
                    self.logger.info(
                        "Resolution is set to '%s', detected MediaInfo resolution is '%s' ('%sx%s'). Ignoring mismatch because of force upload.",
                        self.ReleaseInfo.Resolution,
                        resolution,
                        mediaInfo.Width,
                        mediaInfo.Height,
                    )
                else:
                    raise PtpUploaderException(
                        "Resolution is set to '%s', detected MediaInfo resolution is '%s' ('%sx%s').",
                        self.ReleaseInfo.Resolution,
                        resolution,
                        mediaInfo.Width,
                        mediaInfo.Height,
                    )
        else:
            self.ReleaseInfo.Resolution = resolution

    def __MakeReleaseDescription(self):
        self.ReleaseInfo.AnnouncementSource.ReadNfo(self.ReleaseInfo)

        includeReleaseName = (
            self.ReleaseInfo.AnnouncementSource.IncludeReleaseNameInReleaseDescription()
        )
        outputImageDirectory = (
            self.ReleaseInfo.AnnouncementSource.GetTemporaryFolderForImagesAndTorrent(
                self.ReleaseInfo
            )
        )
        releaseDescriptionFormatter = ReleaseDescriptionFormatter(
            self.ReleaseInfo,
            self.VideoFiles,
            self.AdditionalFiles,
            outputImageDirectory,
            not self.ReleaseInfo.OverrideScreenshots,
        )
        self.ReleaseDescription = releaseDescriptionFormatter.Format(includeReleaseName)
        self.MainMediaInfo = releaseDescriptionFormatter.GetMainMediaInfo()

        # To not waste the uploaded screenshots we commit them to the database because the following function calls can all throw exceptions.
        self.ReleaseInfo.save()

        self.__GetMediaInfoContainer(self.MainMediaInfo)
        self.__GetMediaInfoCodec(self.MainMediaInfo)
        self.__GetMediaInfoResolution(self.MainMediaInfo)

    # Returns with true if failed to detect the language.
    def __DetectSubtitlesAddOne(self, subtitleIds, languageName):
        s_id = MyGlobals.PtpSubtitle.GetId(languageName)
        if s_id is None:
            # TODO: show warning on the WebUI
            self.logger.warning("Unknown subtitle language: '%s'.", languageName)
            return True

        s_id = str(s_id)
        if id not in subtitleIds:
            subtitleIds.append(s_id)

        return False

    def __DetectSubtitles(self):
        subtitleIds = self.ReleaseInfo.GetSubtitles()
        if subtitleIds:
            self.logger.info("Subtitle list is not empty. Skipping subtitle detection.")
            return

        self.logger.info("Detecting subtitles.")

        # We can't do anything with DVD images.
        if self.ReleaseInfo.IsDvdImage():
            raise PtpUploaderException(
                "Unable to automatically detect DVD subtitles, please select them manually"
            )

        containsUnknownSubtitle = False

        # Read from MediaInfo.
        for language in self.MainMediaInfo.Subtitles:
            containsUnknownSubtitle |= self.__DetectSubtitlesAddOne(
                subtitleIds, language
            )

        # Try to read from IDX with the same name as the main video file.
        idxPath, _ = os.path.splitext(self.MainMediaInfo.Path)
        idxPath += ".idx"
        if os.path.isfile(idxPath):
            idxLanguages = GetIdxSubtitleLanguages(idxPath)
            if len(idxLanguages) > 0:
                for language in idxLanguages:
                    containsUnknownSubtitle |= self.__DetectSubtitlesAddOne(
                        subtitleIds, language
                    )
            else:
                containsUnknownSubtitle = True

        # If everything went successfully so far, then check if there are any SRT files in the release.
        if not containsUnknownSubtitle:
            for file in self.AdditionalFiles:
                if str(file).lower().endswith(".srt"):
                    # TODO: show warning on the WebUI
                    containsUnknownSubtitle = True
                    break

        if subtitleIds:
            self.ReleaseInfo.SetSubtitles(subtitleIds)
        elif not containsUnknownSubtitle:
            self.ReleaseInfo.SetSubtitles([str(PtpSubtitleId.NoSubtitle)])

        if not self.ReleaseInfo.GetSubtitles():
            raise PtpUploaderException(
                "Unable to automatically detect subtitles, please select them manually"
            )

    def __MakeTorrent(self):
        if self.ReleaseInfo.UploadTorrentFilePath:
            self.logger.info(
                "Upload torrent file path is set, not making torrent again."
            )
            return

        # We save it into a separate folder to make sure it won't end up in the upload somehow. :)
        uploadTorrentName = "PTP " + self.ReleaseInfo.ReleaseName + ".torrent"
        uploadTorrentFilePath = (
            self.ReleaseInfo.AnnouncementSource.GetTemporaryFolderForImagesAndTorrent(
                self.ReleaseInfo
            )
        )
        uploadTorrentFilePath = os.path.join(uploadTorrentFilePath, uploadTorrentName)

        uploadTorrentCreatePath = ""

        # Make torrent with the parent directory's name included if there is more than one file or requested by the source (it is a scene release).
        totalFileCount = len(self.VideoFiles) + len(self.AdditionalFiles)
        if totalFileCount > 1 or (
            self.ReleaseInfo.AnnouncementSource.IsSingleFileTorrentNeedsDirectory(
                self.ReleaseInfo
            )
            and not self.ReleaseInfo.ForceDirectorylessSingleFileTorrent
        ):
            uploadTorrentCreatePath = self.ReleaseInfo.GetReleaseUploadPath()
        else:  # Create the torrent including only the single video file.
            uploadTorrentCreatePath = self.MainMediaInfo.Path

        Mktor.Make(self.logger, uploadTorrentCreatePath, uploadTorrentFilePath)

        # Local variables are used temporarily to make sure that values only get stored in the database if MakeTorrent.Make succeeded.
        self.ReleaseInfo.UploadTorrentFilePath = uploadTorrentFilePath
        self.ReleaseInfo.UploadTorrentCreatePath = uploadTorrentCreatePath
        self.ReleaseInfo.save()

    def __CheckIfExistsOnPtp(self):
        # TODO: this is temporary here. We should support it everywhere.
        # If we are not logged in here that could mean that the download took a lot of time and the user got logged out for some reason.
        Ptp.Login()

        # This could be before the Ptp.Login() line, but this way we can hopefully avoid some logging out errors.
        if self.ReleaseInfo.IsZeroImdbId():
            self.logger.info(
                "IMDb ID is set zero, ignoring the check for existing release."
            )
            return

        movieOnPtpResult = None

        if self.ReleaseInfo.PtpId:
            movieOnPtpResult = Ptp.GetMoviePageOnPtp(
                self.logger, self.ReleaseInfo.PtpId
            )
        else:
            movieOnPtpResult = Ptp.GetMoviePageOnPtpByImdbId(
                self.logger, self.ReleaseInfo.ImdbId
            )
            self.ReleaseInfo.PtpId = movieOnPtpResult.PtpId

        # Check (again) if is it already on PTP.
        existingRelease = movieOnPtpResult.IsReleaseExists(self.ReleaseInfo)
        if (
            existingRelease is not None
            and int(existingRelease["Id"]) > self.ReleaseInfo.DuplicateCheckCanIgnore
        ):
            raise PtpUploaderException(
                JobRunningState.DownloadedAlreadyExists,
                "Got uploaded to PTP while we were working on it. Skipping upload because of format '%s'."
                % existingRelease["FullTitle"],
            )

    def __CheckSynopsis(self):
        if (
            Settings.StopIfSynopsisIsMissing
            and Settings.StopIfSynopsisIsMissing.lower() == "beforeuploading"
        ):
            self.ReleaseInfo.AnnouncementSource.CheckSynopsis(
                self.logger, self.ReleaseInfo
            )

    def __CheckCoverArt(self):
        self.ReleaseInfo.AnnouncementSource.CheckCoverArt(self.logger, self.ReleaseInfo)

    def __RehostPoster(self):
        # If this movie has no page yet on PTP then we will need the cover, so we rehost the image to an image hoster.
        if self.ReleaseInfo.PtpId or (not self.ReleaseInfo.CoverArtUrl):
            return

        # Rehost the image only if not already on an image hoster.
        url = self.ReleaseInfo.CoverArtUrl
        if url.find("ptpimg.me") != -1 or url.find("picload.org") != -1:
            return

        self.logger.info("Rehosting poster from '%s'.", url)
        self.ReleaseInfo.CoverArtUrl = ImageUploader.Upload(self.logger, imageUrl=url)
        self.logger.info("Rehosted poster to '%s'.", self.ReleaseInfo.CoverArtUrl)

    def __StopBeforeUploading(self):
        if self.ReleaseInfo.StopBeforeUploading:
            raise PtpUploaderException("Stopping before uploading.")

    def __StartTorrent(self):
        if len(self.ReleaseInfo.UploadTorrentInfoHash) > 0:
            self.logger.info(
                "Upload torrent info hash is set, not starting torrent again."
            )
            return

        # Add torrent without hash checking.
        self.ReleaseInfo.UploadTorrentInfoHash = (
            self.TorrentClient.AddTorrentSkipHashCheck(
                self.logger,
                self.ReleaseInfo.UploadTorrentFilePath,
                self.ReleaseInfo.GetReleaseUploadPath(),
            )
        )
        self.ReleaseInfo.save()

    def __UploadMovie(self):
        # This is not possible because finished jobs can't be restarted.
        if self.ReleaseInfo.IsJobPhaseFinished(FinishedJobPhase.Upload_UploadMovie):
            self.logger.info(
                "Upload movie phase has been reached previously, not uploading it again."
            )
            return

        Ptp.UploadMovie(
            self.logger,
            self.ReleaseInfo,
            self.ReleaseInfo.UploadTorrentFilePath,
            self.ReleaseDescription,
        )

        # Delete the source torrent file.
        if self.ReleaseInfo.SourceTorrentFilePath and os.path.isfile(
            self.ReleaseInfo.SourceTorrentFilePath
        ):
            os.remove(self.ReleaseInfo.SourceTorrentFilePath)
            self.ReleaseInfo.SourceTorrentFilePath = ""

        # Delete the uploaded torrent file.
        if self.ReleaseInfo.UploadTorrentFilePath and os.path.isfile(
            self.ReleaseInfo.UploadTorrentFilePath
        ):
            os.remove(self.ReleaseInfo.UploadTorrentFilePath)
            self.ReleaseInfo.UploadTorrentFilePath = ""

        jobDuration = TimeDifferenceToText(
            datetime.datetime.utcnow() - self.ReleaseInfo.JobStartTimeUtc, 10, "", "0s"
        )
        self.logger.info(
            "'%s' has been successfully uploaded to PTP. Time taken: %s.",
            self.ReleaseInfo.ReleaseName,
            jobDuration,
        )

        self.ReleaseInfo.SetJobPhaseFinished(FinishedJobPhase.Upload_UploadMovie)
        self.ReleaseInfo.JobRunningState = JobRunningState.Finished
        self.ReleaseInfo.save()

    def __ExecuteCommandOnSuccessfulUpload(self):
        # Execute command on successful upload.
        if Settings.OnSuccessfulUpload is None or len(Settings.OnSuccessfulUpload) <= 0:
            return

        uploadedTorrentUrl = (
            "https://passthepopcorn.me/torrents.php?id=" + self.ReleaseInfo.PtpId
        )
        command = Settings.OnSuccessfulUpload % {
            "releaseName": self.ReleaseInfo.ReleaseName,
            "uploadPath": self.ReleaseInfo.UploadTorrentCreatePath,
            "uploadedTorrentUrl": uploadedTorrentUrl,
        }

        # We don't care if this fails. Our upload is complete anyway. :)
        try:
            subprocess.Popen(command, shell=True)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.logger.exception(
                "Got exception while trying to run command '%s' after successful upload.",
                command,
            )
