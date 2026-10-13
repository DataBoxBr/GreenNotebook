# GreenNotebook

GreenNotebook is a customized fork of RedNotebook. It lets you format, tag and
search your entries. You can also add pictures, links and customizable
templates, spell check your notes, and export to plain text, HTML,
Latex or PDF.


Windows build artifacts are generated in the local `dist` directory.


## Using a synced journal folder

In **Preferences > Journal base folder**, choose a local folder managed by
Google Drive. **Open selected journal** opens the journal there, or starts an
empty journal. **Copy current journal** copies your current journal and its
complete revision history into an empty destination outside the current journal
folder. The selected local path is remembered on each device across restarts.

Sync the entire journal folder, including `greennotebook-revisions`, and use the
same upgraded GreenNotebook version on all devices. Wait for Google Drive's
initial sync to finish and all files to be downloaded locally before editing.
A month whose initial revisions are incomplete remains read-only until the
required files arrive. Already loaded months retain local edits while waiting
for any temporarily missing revisions.

Saves create branches of immutable JSON revision records and rebuild monthly
YAML caches (`YYYY-MM.txt`) with a `# greennotebook-cache-v1` comment header.
Revision files are authoritative. Do not manually edit the monthly YAML caches
or use older clients with this folder. Deleting revision files loses history
and recovery data.

GreenNotebook polls the local folder every 10 seconds and when the window gains
focus, autosaving local edits and reconciling revisions. It has no Google Drive
API integration and cannot verify cloud uploads or downloads. **Saved locally**
does not mean **synced**; check the Google Drive client for transfer progress.

Offline edits on multiple devices retain both contributions. Once all revisions
arrive, changes are merged; overlapping edits preserve both versions with
conflict markers. Review the text before resolving conflicts, and use **Sync
details** to inspect revision state and conflict details shown as JSON.

Keep `greennotebook-revisions` intact: history is never garbage-collected, so the
folder grows over time. **Journal > Backup** creates a ZIP that includes revision
history, but only files inside the journal folder, not externally linked files.
Journal files and backup ZIPs are not encrypted by GreenNotebook.


## Requirements

Needed for running GreenNotebook:

  * GTK (3.24): https://www.gtk.org
  * GtkSourceView (3.0+): https://wiki.gnome.org/Projects/GtkSourceView
  * Python (3.8+): https://www.python.org
  * PyYAML (3.10+): https://pyyaml.org
  * WebKitGTK (2.16+): https://webkitgtk.org (only on Linux and macOS)
  * PyEnchant for spell checking (1.6+): https://pypi.org/project/pyenchant/ (optional)

Needed for installing GreenNotebook:

  * GNU gettext: https://www.gnu.org/software/gettext
  * Setuptools (60.0+): https://pypi.org/project/setuptools


## Run from source

Install all dependencies:

  * Linux/macOS: [run-tests.yml](.github/workflows/run-tests.yml)
  * Windows: [build-windows.yml](.github/workflows/build-windows.yml)

Start GreenNotebook:

  * Linux/macOS: `python3 rednotebook/journal.py`
  * Windows: `py rednotebook/journal.py`


## Set up pre-commit hooks

Install [pre-commit](https://pre-commit.com/), then run `pre-commit install`.


## Run tests

Install [tox](https://tox.wiki), then run `tox`.


## Thanks to

  * The authors of the libraries listed under 'Requirements'.
  * Ciaran for creating the original RedNotebook icon used by this derivative.
  * The [txt2tags](https://txt2tags.org) team for their markup conversion tool.
  * Dieter Verfaillie for his [elib.intl](https://github.com/dieterv/elib.intl) module.
  * Maximilian Köhl for his [pygtkspellcheck](https://github.com/koehlma/pygtkspellcheck) project.
  * The Weblate team for hosting [translations for RedNotebook](https://hosted.weblate.org/engage/rednotebook/).


## License notes

GreenNotebook is derived from RedNotebook, which is published under the GPLv2+. Since it bundles code
released under the LGPLv3+, the resulting work is licensed under the
GPLv3+. See `debian/copyright` for detailed license information.


Enjoy!
