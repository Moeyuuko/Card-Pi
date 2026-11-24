"""Application-local Rime frontend using the versioned C API."""
import ctypes as C
from ctypes.util import find_library
from dataclasses import dataclass
import os

I, S, P, ID = C.c_int, C.c_char_p, C.c_void_p, C.c_size_t
CHINESE_PUNCTUATION = dict(zip(",.!?;:()[]", "，。！？；：（）【】"))
FULL_WIDTH = str.maketrans({chr(code): chr(code + 0xFEE0) for code in range(33, 127)} | {" ": "　"})


@dataclass(frozen=True)
class PinyinOptions:
    script: str = "simplified"
    scheme: str = "full"
    candidates: int = 5
    punctuation: str = "ascii"
    width: str = "half"
    fuzzy: bool = False
    completion: bool = True
    sentences: bool = True
    learning: bool = True

    @classmethod
    def from_settings(cls, settings):
        defaults = cls()
        saved = settings.get("pinyin")
        if not isinstance(saved, dict):
            return defaults
        choices = {
            "script": ("simplified", "traditional"), "scheme": ("full", "double"),
            "candidates": (3, 4, 5), "punctuation": ("ascii", "chinese"),
            "width": ("half", "full"), "fuzzy": (False, True),
            "completion": (False, True), "sentences": (False, True), "learning": (False, True),
        }
        return cls(**{key: saved[key] for key, values in choices.items()
                      if key in saved and type(saved[key]) is type(values[0]) and saved[key] in values})

    @property
    def schema(self):
        if self.scheme == "double":
            return "double_pinyin"
        return "pinyin_simp" if self.script == "simplified" else "luna_pinyin"


def managed_config(path, content):
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return True



class Versioned(C.Structure):
    _fields_ = [("data_size", I)]

    def __init__(self):
        super().__init__()
        self.data_size = C.sizeof(self) - C.sizeof(I)


class Traits(Versioned):
    _fields_ = [(key, S) for key in (
        "shared_data_dir", "user_data_dir", "distribution_name",
        "distribution_code_name", "distribution_version", "app_name",
    )] + [("modules", P), ("min_log_level", I), ("log_dir", S),
          ("prebuilt_data_dir", S), ("staging_dir", S)]


class Composition(C.Structure):
    _fields_ = [(key, I) for key in ("length", "cursor_pos", "sel_start", "sel_end")] + [
        ("preedit", S)]


class Candidate(C.Structure):
    _fields_ = [("text", S), ("comment", S), ("reserved", P)]


class Menu(C.Structure):
    _fields_ = [(key, I) for key in (
        "page_size", "page_no", "is_last_page", "highlighted", "count",
    )] + [("candidates", C.POINTER(Candidate)), ("select_keys", S)]


class Context(Versioned):
    _fields_ = [("composition", Composition), ("menu", Menu),
                ("commit_preview", S), ("select_labels", P)]


class Commit(Versioned):
    _fields_ = [("text", S)]


class API(Versioned):
    # Fields must follow the ABI order, including unused entries.
    _fields_ = [
        ("setup", C.CFUNCTYPE(None, C.POINTER(Traits))),
        ("notify", P),
        ("initialize", C.CFUNCTYPE(None, C.POINTER(Traits))),
        ("finalize", C.CFUNCTYPE(None)),
        ("maintain", C.CFUNCTYPE(I, I)),
        ("is_maintaining", P),
        ("join", C.CFUNCTYPE(None)),
        ("deployer_initialize", P), ("prebuild", P), ("deploy", P),
        ("deploy_schema", C.CFUNCTYPE(I, S)),
        ("deploy_config", P), ("sync", P),
        ("create", C.CFUNCTYPE(ID)),
        ("find", P),
        ("destroy", C.CFUNCTYPE(I, ID)),
        ("cleanup_stale", P), ("cleanup_all", P),
        ("key", C.CFUNCTYPE(I, ID, I, I)),
        ("commit", C.CFUNCTYPE(I, ID)),
        ("clear", C.CFUNCTYPE(None, ID)),
        ("get_commit", C.CFUNCTYPE(I, ID, C.POINTER(Commit))),
        ("free_commit", C.CFUNCTYPE(I, C.POINTER(Commit))),
        ("get_context", C.CFUNCTYPE(I, ID, C.POINTER(Context))),
        ("free_context", C.CFUNCTYPE(I, C.POINTER(Context))),
        ("get_status", P), ("free_status", P),
        ("set_option", C.CFUNCTYPE(None, ID, S, I)),
        ("get_option", P), ("set_property", P), ("get_property", P),
        ("get_schema_list", P), ("free_schema_list", P), ("current_schema", P),
        ("select_schema", C.CFUNCTYPE(I, ID, S)),
    ]


class Pinyin:
    def __init__(self, directory, options=PinyinOptions()):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        shared = os.environ.get("CARD_PI_RIME_DATA", "/usr/share/rime-data")
        library = find_library("rime") or "librime.so.1"
        self.library = C.CDLL(library)
        self.library.rime_get_api.restype = C.POINTER(API)
        self.api = self.library.rime_get_api().contents
        if self.api.data_size + C.sizeof(I) < C.sizeof(API):
            raise RuntimeError("Rime API too old")
        traits = Traits()
        traits.shared_data_dir = os.fsencode(shared)
        traits.user_data_dir = os.fsencode(directory)
        traits.app_name = b"rime.card-pi"
        traits.min_log_level = 2
        traits.log_dir = b""
        self.api.setup(C.byref(traits))
        self.api.initialize(C.byref(traits))
        self.session = 0
        try:
            self.configure(options)
        except Exception:
            self.close()
            raise

    def configure(self, options):
        schema = options.schema
        default = f"patch:\n  schema_list:\n    - schema: {schema}\n  menu/page_size: {options.candidates}\n"
        schema_patch = (f"patch:\n  menu/page_size: {options.candidates}\n"
                        "  editor/bindings/Return: commit_raw_input\n")
        for key, value in (
            ("enable_completion", options.completion),
            ("enable_sentence", options.sentences),
            ("enable_user_dict", options.learning),
        ):
            schema_patch += f"  translator/{key}: {str(value).lower()}\n"
        if options.fuzzy and options.scheme == "full":
            schema_patch += (
                "  speller/algebra/+:\n"
                "    - derive/^([zcs])h/$1/\n"
                "    - derive/^([zcs])([^h])/$1h$2/\n"
                "    - derive/([aei])ng$/$1n/\n"
                "    - derive/([aei])n$/$1ng/\n"
            )
        default_changed = managed_config(self.directory / "default.custom.yaml", default)
        schema_changed = managed_config(self.directory / f"{schema}.custom.yaml", schema_patch)
        changed = default_changed or schema_changed
        if self.session:
            self.api.destroy(self.session)
            self.session = 0
            if changed:
                # Reload the registry before deploying a different schema or prism.
                self.api.finalize()
                self.api.initialize(None)
        # Rime compares config mtimes at second precision. Force rebuilds for the
        # generated configs we changed, including rapid edits within one second.
        if default_changed:
            (self.directory / "build/default.yaml").unlink(missing_ok=True)
        if schema_changed:
            (self.directory / "build" / f"{schema}.schema.yaml").unlink(missing_ok=True)
        if self.api.maintain(int(changed)):
            self.api.join()
        self.session = self.api.create()
        if not self.session or not self.api.select_schema(self.session, schema.encode()):
            raise RuntimeError(f"{schema} schema unavailable")
        self.api.set_option(self.session, b"ascii_punct", options.punctuation == "ascii")
        self.api.set_option(self.session, b"full_shape", options.width == "full")
        self.api.set_option(self.session, b"zh_hans", False)
        self.api.set_option(self.session, b"simplification",
                            options.scheme == "double" and options.script == "simplified")
        self.options = options
        return self

    def literal(self, text):
        if self.options.punctuation == "chinese" and text in CHINESE_PUNCTUATION:
            return CHINESE_PUNCTUATION[text]
        return text.translate(FULL_WIDTH) if self.options.width == "full" else text

    def close(self):
        if self.session:
            self.api.destroy(self.session)
            self.session = 0
        self.api.finalize()

    def clear(self):
        self.api.clear(self.session)

    def view(self):
        ctx = Context()
        if not self.api.get_context(self.session, C.byref(ctx)):
            return "", [], 0
        try:
            preedit = (ctx.composition.preedit or b"").decode("utf-8")
            candidates = [ctx.menu.candidates[i].text.decode("utf-8")
                          for i in range(min(ctx.menu.count, self.options.candidates))]
            return preedit, candidates, ctx.menu.highlighted
        finally:
            self.api.free_context(C.byref(ctx))

    def key(self, code):
        handled = self.api.key(self.session, code, 0)
        value = Commit()
        if not self.api.get_commit(self.session, C.byref(value)):
            return handled, ""
        try:
            return handled, (value.text or b"").decode("utf-8")
        finally:
            self.api.free_commit(C.byref(value))
