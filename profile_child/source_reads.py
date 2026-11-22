"""Classify file opens initiated by project code during baseline tracing."""


def direct_reader(frame, project_path):
    # A loader between open() and the first project frame is fetching code
    # to execute. An explicit read_text(), for example, has no such frame.
    while frame:
        module = frame.f_globals.get("__name__", "")
        if module.startswith(("importlib.", "_frozen_importlib", "_pytest.")) or module == "runpy":
            return False
        if project_path(frame.f_code.co_filename):
            return True
        frame = frame.f_back
    return False
