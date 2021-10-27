from flask import redirect, render_template, request, url_for

from PtpUploader.Job.JobRunningState import JobRunningState
from PtpUploader.MyGlobals import MyGlobals
from PtpUploader.PtpUploaderMessage import *
from PtpUploader.ReleaseInfo import ReleaseInfo
from PtpUploader.Settings import Settings
from PtpUploader.WebServer import app
from PtpUploader.WebServer.Authentication import requires_auth
from PtpUploader.WebServer.JobCommon import JobCommon


@app.route("/job/<int:jobId>/edit/", methods=["GET", "POST"])
@requires_auth
def EditJob(jobId):
    if request.method == "POST":
        releaseInfo = ReleaseInfo.objects.get(Id=jobId)

        # TODO: This is very far from perfect. There is no guarantee that the job didn't start meanwhile.
        # Probably only the WorkerThread should change the running state.
        if not releaseInfo.CanEdited():
            return "The job is currently running and can't be edited!"

        releaseInfo.SetStopBeforeUploading(
            request.values["post"] == "Resume but stop before uploading"
        )

        if releaseInfo.IsReleaseNameEditable():
            releaseInfo.ReleaseName = request.values["release_name"]

        JobCommon.FillReleaseInfoFromRequestData(releaseInfo, request)
        releaseInfo.JobRunningState = JobRunningState.WaitingForStart
        releaseInfo.save()
        MyGlobals.PtpUploader.add_message(PtpUploaderMessageStartJob(releaseInfo.Id))

        return redirect(url_for("jobs"))

    releaseInfo = ReleaseInfo.objects.get(Id=jobId)
    job = {}
    JobCommon.FillDictionaryFromReleaseInfo(job, releaseInfo)

    if releaseInfo.CanEdited():
        if releaseInfo.IsReleaseNameEditable():
            job["IsReleaseNameEditable"] = True

        job["CanBeEdited"] = True

    settings = {}
    if Settings.OpenJobPageLinksInNewTab == "0":
        settings["OpenJobPageLinksInNewTab"] = ""
    else:
        settings["OpenJobPageLinksInNewTab"] = ' target="_blank"'

    source = MyGlobals.SourceFactory.GetSource(releaseInfo.AnnouncementSourceName)
    if source is not None:
        filename = "source_icon/%s.ico" % releaseInfo.AnnouncementSourceName
        job["SourceIcon"] = url_for("static", filename=filename)
        job["SourceUrl"] = source.GetUrlFromId(releaseInfo.AnnouncementId)

    return render_template("edit_job.html", job=job, settings=settings)
