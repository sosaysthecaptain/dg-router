"""KiCad Action Plugin registration (SWIG/pcbnew API).

This is the classic mechanism: KiCad discovers plugins in its scripting/plugins
dir, and each ActionPlugin becomes a toolbar button in the PCB editor.

Kept deliberately thin — all real work lives in shim.py (headless-safe) and
dialog.py (wx UI), so the logic is triggerable from code without the GUI.
"""

import os

import pcbnew


class DgRouterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "dg-router (AI autorouter)"
        self.category = "Routing"
        self.description = (
            "AI-drivable incremental autorouter — Milestone 0 shim "
            "(inspect nets, preview render, emit job.json; no routing yet)"
        )
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), "icon.png")
        self.icon_file_name = icon
        self.dark_icon_file_name = icon

    def Run(self):
        # Hot-reload every submodule (dependency order — dialog imports the
        # others, so reload it LAST) so editing any of them takes effect the next
        # time you click the toolbar button — no KiCad restart needed.
        import importlib
        from . import shim, router, placement, dialog
        for mod in (shim, router, placement, dialog):
            importlib.reload(mod)
        dialog.show_dialog(pcbnew.GetBoard())
