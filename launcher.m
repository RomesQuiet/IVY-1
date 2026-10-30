// launcher.m — the native executable inside Ivy.app.
//
// macOS decides who is asking for the microphone from the running process. A shell
// script that exec's Python turns the process into Python.app, which has no microphone
// description, so the request is refused silently: no pop-up, and Ivy never appears in
// Privacy & Security. This binary runs Python inside its own process instead, so macOS
// sees Ivy.app, with its Info.plist, name and icon.
//
// Built by make_app.sh, which supplies the IVY_* paths below.

#import <AVFoundation/AVFoundation.h>
#include <Python.h>
#include <stdio.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define LOG_LIMIT (2 * 1024 * 1024)

static void open_log(void) {
    struct stat info;
    if (stat(IVY_LOG, &info) == 0 && info.st_size > LOG_LIMIT) {
        rename(IVY_LOG, IVY_LOG ".old");
    }
    freopen(IVY_LOG, "a", stdout);
    freopen(IVY_LOG, "a", stderr);
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);

    time_t now = time(NULL);
    printf("\n=== Ivy launch: %s", ctime(&now));
}

// Ask right away, so the pop-up appears and Ivy joins the Microphone list on first launch.
static void request_microphone(void) {
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    printf("Microphone permission status: %ld (0 undecided, 1 restricted, 2 denied, 3 allowed)\n",
           (long)status);
    if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) {
            printf("Microphone permission answer: %s\n", granted ? "allowed" : "denied");
        }];
    }
}

int main(void) {
    if (chdir(IVY_PROJECT) != 0) {
        return 1;
    }
    open_log();
    request_microphone();

    // Makes Python use the project's venv, and keeps sys.executable pointing at a real
    // Python so the run_python tool can start scripts.
    setenv("__PYVENV_LAUNCHER__", IVY_PYTHON, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    char *arguments[] = {IVY_PYTHON, IVY_ENTRY};
    return Py_BytesMain(2, arguments);
}
