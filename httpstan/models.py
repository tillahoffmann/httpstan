"""Compile a Stan model extension module given code written in Stan.

These functions manage the process of compiling a Python extension module
from C++ code generated and loading the resulting module.
"""
import asyncio
import contextlib
import functools
import hashlib
import importlib
import io
import logging
import os
import pathlib
import platform
import shutil
import string
import sqlite3
import sys
import tempfile
from types import ModuleType
from typing import Any, Generator, TextIO, Tuple, List, Optional, IO

# IMPORTANT: `import setuptools` MUST come before any module imports `distutils`
# background: bugs.python.org/issue23114
import setuptools
import Cython
import Cython.Build
import Cython.Build.Inline
import pkg_resources

import httpstan.cache
import httpstan.compile
import httpstan.stan

PACKAGE_DIR = pathlib.Path(__file__).resolve(strict=True).parents[0]
logger = logging.getLogger("httpstan")


@contextlib.contextmanager
def TemporaryDirectory(
    suffix: Optional[str] = None, prefix: Optional[str] = None, dir: Optional[str] = None
) -> Generator[str, None, None]:
    """Mimic tempfile.TemporaryDirectory with one Windows-specific cleanup fix."""
    name = tempfile.mkdtemp(suffix, prefix, dir)
    yield name
    # ignore_errors=True is important for Windows. Windows will encounter an
    # `Access denied` error if the standard library TemporaryDirectory is used.
    shutil.rmtree(name, ignore_errors=True)


def calculate_model_name(program_code: str) -> str:
    """Calculate model name from Stan program code.

    Names look like this: ``models/cb6777c3543ebf18``. Name uses a hash of the
    concatenation of the following:

    - UTF-8 encoded Stan program code
    - UTF-8 encoded string recording the current Stan version
    - UTF-8 encoded string recording the current httpstan version
    - UTF-8 encoded string identifying the current system platform

    Arguments:
        program_code: Stan program code.

    Returns:
        str: model name

    """
    # digest_size of 5 means we expect a collision after a million models
    digest_size = 5
    hash = hashlib.blake2b(digest_size=digest_size)
    hash.update(program_code.encode())
    hash.update(httpstan.stan.version().encode())
    hash.update(httpstan.__version__.encode())
    hash.update(sys.platform.encode())
    return f"models/{hash.hexdigest()}"


def calculate_module_name(model_name: str) -> str:
    """Calculate module name from `model_name`.

    The module name must be a valid Python identifier. This means it must obey
    rules which `model_name` does not follow. For example, it cannot contain a
    forward slash.

    Arguments:
        model_name

    Returns:
        str: module name derived from `model_name`.

    """
    model_id = model_name.split("/")[-1]
    return f"model_{model_id}"


async def compile_model_extension_module(program_code: str) -> Tuple[bytes, str]:
    """Compile extension module for a Stan model.

    Returns bytes of the compiled module.

    Since compiling a Stan model extension module takes a long time,
    compilation takes place in a different thread.

    This is a coroutine function.

    Returns:
        bytes: binary representation of module.
        str: Output (standard error) from compiler.

    """
    model_name = calculate_model_name(program_code)
    # use the module name as the Stan model name
    stan_model_name = calculate_module_name(model_name)
    logger.info(f"compiling cpp for `{model_name}`.")
    cpp_code = await asyncio.get_event_loop().run_in_executor(
        None, httpstan.compile.compile, program_code, stan_model_name
    )
    pyx_code_template = pkg_resources.resource_string(__name__, "anonymous_stan_model_services.pyx.template").decode()
    logger.info(f"building extension module with stan model name `{stan_model_name}`.")
    module_bytes, compiler_output = _build_extension_module(stan_model_name, cpp_code, pyx_code_template)
    return module_bytes, compiler_output


def _import_module(module_name: str, module_path: str) -> ModuleType:
    """Load the module named `module_name` from  `module_path`.

    Arguments:
        module_name: module name.
        module_path: module path.

    Returns:
        module: Loaded module.

    """
    sys.path.append(module_path)
    module = importlib.import_module(module_name)
    sys.path.pop()
    return module


async def import_model_extension_module(model_name: str, db: sqlite3.Connection) -> Tuple[ModuleType, str]:

    """Load an existing Stan model extension module.

    Arguments:
        model_name

    Returns:
        module: loaded module handle.
        str: Compiler output.

    Raises:
        KeyError: Model not found.

    """
    # may raise KeyError
    module_bytes, compiler_output = await httpstan.cache.load_model_extension_module(model_name, db)
    # NOTE: module suffix can be '.so'/'.pyd'; does not need to be, say,
    # '.cpython-36m-x86_64-linux-gnu.so'.  The module's filename (minus any
    # suffix) does matter: Python calls an initialization function using the
    # module name, e.g., PyInit_mymodule.  Filenames which do not match the name
    # of this function will not load.
    module_name = calculate_module_name(model_name)
    # On Windows the correct module suffix is '.pyd'
    # See https://docs.python.org/3/faq/windows.html#is-a-pyd-file-the-same-as-a-dll
    module_filename = f"{module_name}{'.so' if platform.system() != 'Windows' else '.pyd'}"
    assert isinstance(module_bytes, bytes)

    with TemporaryDirectory(prefix="httpstan_") as temporary_directory:
        with open(os.path.join(temporary_directory, module_filename), "wb") as fh:
            fh.write(module_bytes)
        module_path = temporary_directory
        assert module_name == os.path.splitext(module_filename)[0]
        return _import_module(module_name, module_path), compiler_output


@functools.lru_cache()
def _build_extension_module(
    module_name: str, cpp_code: str, pyx_code_template: str, extra_compile_args: Optional[List[str]] = None,
) -> Tuple[bytes, str]:
    """Build extension module and return its name and binary representation.

    This returns the module name and bytes (!) of a Python extension module. The
    module is not loaded by this function.

    `cpp_code` and `pyx_code_template` are written to
    ``model_{model_id}.hpp`` and `model_{model_id}.pyx` respectively.

    The string `pyx_code_template` must contain the string ``${cpp_filename}``
    which will be replaced by ``model_{model_id}.hpp``.

    The module name is a deterministic function of the `model_name`.

    Arguments:
        model_name
        cpp_code
        pyx_code_template: string passed to ``string.Template``.
        extra_compile_args

    Returns:
        bytes: binary representation of module.
        str: Output (standard error) from compiler.

    """

    # define utility functions for silencing compiler output
    def _has_fileno(stream: TextIO) -> bool:
        """Returns whether the stream object has a working fileno()

        Suggests whether _redirect_stderr is likely to work.
        """
        try:
            stream.fileno()
        except (AttributeError, OSError, IOError, io.UnsupportedOperation):
            return False
        return True

    def _redirect_stdout() -> int:
        """Redirect stdout for subprocesses to /dev/null.

        Returns
        -------
        orig_stderr: copy of original stderr file descriptor
        """
        sys.stdout.flush()
        stdout_fileno = sys.stdout.fileno()
        orig_stdout = os.dup(stdout_fileno)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stdout_fileno)
        os.close(devnull)
        return orig_stdout

    def _redirect_stderr_to(stream: IO[Any]) -> int:
        """Redirect stderr for subprocesses to /dev/null.

        Returns
        -------
        orig_stderr: copy of original stderr file descriptor
        """
        sys.stderr.flush()
        stderr_fileno = sys.stderr.fileno()
        orig_stderr = os.dup(stderr_fileno)
        os.dup2(stream.fileno(), stderr_fileno)
        return orig_stderr

    # write files need for compilation in a temporary directory which will be
    # removed when this function exits.
    with TemporaryDirectory(prefix="httpstan_") as temp_dir:
        temporary_directory = pathlib.Path(temp_dir)
        cpp_filepath = temporary_directory / f"{module_name}.hpp"
        pyx_filepath = temporary_directory / f"{module_name}.pyx"
        pyx_code = string.Template(pyx_code_template).substitute(cpp_filename=cpp_filepath.as_posix())
        for filepath, code in zip([cpp_filepath, pyx_filepath], [cpp_code, pyx_code]):
            with open(filepath, "w") as fh:
                fh.write(code)

        httpstan_dir = os.path.dirname(__file__)
        callbacks_writer_pb_filepath = pathlib.Path(httpstan_dir) / "callbacks_writer.pb.cc"
        include_dirs = [
            httpstan_dir,  # for queue_writer.hpp and queue_logger.hpp
            temporary_directory.as_posix(),
            os.path.join(httpstan_dir, "include"),
            os.path.join(httpstan_dir, "include", "lib", "eigen_3.3.3"),
            os.path.join(httpstan_dir, "include", "lib", "boost_1.69.0"),
            os.path.join(httpstan_dir, "include", "lib", "sundials_4.1.0", "include"),
        ]

        stan_macros: List[Tuple[str, Optional[str]]] = [
            ("BOOST_DISABLE_ASSERTS", None),
            ("BOOST_PHOENIX_NO_VARIADIC_EXPRESSION", None),
            ("STAN_THREADS", None),
            # the following is needed on linux for compatibility with libraries built with the manylinux2014 image
            ("_GLIBCXX_USE_CXX11_ABI", "0"),
        ]

        if extra_compile_args is None:
            extra_compile_args = ["-O3", "-std=c++14"]

        cython_include_path = [os.path.dirname(httpstan_dir)]
        # Note: `library_dirs` is only relevant for linking. It does not tell an extension
        # where to find shared libraries during execution. There are two ways for an
        # extension module to find shared libraries: LD_LIBRARY_PATH and rpath.
        extension = setuptools.Extension(
            module_name,
            language="c++",
            sources=[pyx_filepath.as_posix(), callbacks_writer_pb_filepath.as_posix()],
            define_macros=stan_macros,
            include_dirs=include_dirs,
            library_dirs=[f"{PACKAGE_DIR / 'lib'}"],
            libraries=["protobuf-lite"],
            extra_compile_args=extra_compile_args,
            extra_link_args=[f"-Wl,-rpath,{PACKAGE_DIR / 'lib'}"],
        )
        build_extension = Cython.Build.Inline._get_build_extension()

        # silence stdout and stderr for compilation, if stderr is silenceable
        # silence stdout too as cythonizing prints a couple of lines to stdout
        stream = tempfile.TemporaryFile(prefix="httpstan_")
        redirect_stderr = _has_fileno(sys.stderr)
        compiler_output = ""
        if redirect_stderr:
            orig_stdout = _redirect_stdout()
            orig_stderr = _redirect_stderr_to(stream)
        try:
            build_extension.extensions = Cython.Build.cythonize([extension], include_path=cython_include_path)
            build_extension.build_temp = build_extension.build_lib = temporary_directory.as_posix()
            build_extension.run()
        finally:
            if redirect_stderr:
                stream.seek(0)
                compiler_output = stream.read().decode()
                stream.close()
                # restore
                os.dup2(orig_stderr, sys.stderr.fileno())
                os.dup2(orig_stdout, sys.stdout.fileno())

        module = _import_module(module_name, build_extension.build_lib)
        with open(module.__file__, "rb") as fh:  # type: ignore  # see mypy#3062
            assert module.__name__ == module_name, (module.__name__, module_name)
            return fh.read(), compiler_output  # type: ignore  # see mypy#3062