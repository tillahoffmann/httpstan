from distutils.extension import Extension
from distutils.command.build_ext import build_ext
import pathlib
import shutil
import subprocess


# empty extension module so build machinery recognizes package as platform-specific
extensions = [
    Extension(
        "httpstan.empty",
        sources=["httpstan/empty.cpp"],
        # `make` will download and place `pybind11` in `httpstan/include`
        include_dirs=["httpstan/include"],
        language="c++",
        # -fvisibility=hidden required by pybind11
        extra_compile_args=["-fvisibility=hidden", "-std=c++14"],
    )
]


class BuildExtCommand(build_ext):
    def run(self) -> None:
        # Copy the makefiles and compile the libraries.
        build_lib = pathlib.Path(self.build_lib)
        filenames = ['Makefile', 'Makefile.libraries']
        for filename in filenames:
            shutil.copy(filename, build_lib / filename)
        subprocess.check_call(['make'], cwd=self.build_lib)
        super().run()
        # Remove the `build` library so it doesn't get packaged.
        shutil.rmtree(build_lib / 'build')


def build(setup_kwargs):
    setup_kwargs.update({
        "ext_modules": extensions,
        "cmdclass": {
            "build_ext": BuildExtCommand,
        },
    })
