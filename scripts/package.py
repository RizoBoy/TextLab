"""Package the executable, source, tests and instructions without test data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parent.parent
output = root / "TextLab_Windows.zip"
files = [root / name for name in ["README.md", "run.py", "requirements-build.txt", "Запустить TextLab.cmd", "dist/TextLab.exe"]]
for folder, patterns in [("textlab", ["*.py"]), ("tests", ["*.py"]), ("docs", ["*.md"]), ("scripts", ["*.py", "*.ps1"])]:
    for pattern in patterns:
        files.extend((root / folder).glob(pattern))
with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=6) as archive:
    for file in sorted(files):
        archive.write(file, "TextLab/" + file.relative_to(root).as_posix())
with ZipFile(output) as archive:
    assert archive.testzip() is None
    print(f"{output}: {len(archive.namelist())} files, {output.stat().st_size:,} bytes")
