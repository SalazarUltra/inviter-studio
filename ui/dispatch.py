"""Only the Tk thread drains this queue; worker callbacks never call Tcl."""

from queue import SimpleQueue


class UIDispatcher:
    def __init__(self):
        self.queue = SimpleQueue()
        self.closed = False

    def post(self, callback):
        if not self.closed:
            self.queue.put(callback)

    def drain(self, on_error, limit=200):
        for _ in range(limit):
            if self.queue.empty():
                break
            callback = self.queue.get_nowait()
            if self.closed:
                continue
            try:
                callback()
            except Exception as exc:
                on_error(exc)


def post_ui(widget, callback):
    # _root() traverses Python widget references and does not invoke Tcl.
    root = widget._root()
    dispatcher = getattr(root, "ui_dispatcher", None)
    if dispatcher:

        def guarded():
            if widget.winfo_exists():
                callback()

        dispatcher.post(guarded)
