"""Aceite de checar_texto / --texto (import via caminho: o módulo tem hífen)."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "checar_caminhos", _ROOT / "checar-caminhos.py"
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
checar_texto = _mod.checar_texto

# Partido de propósito: o pre-commit roda checar-caminhos no próprio arquivo de teste.
_FRAG = "/" + "home" + "/" + "alguem" + "/Valt/x.md"


class ChecarTextoTest(unittest.TestCase):
    def test_linha_mais_com_caminho_fragil(self):
        blob = f"*** Begin Patch\n*** Add File: x.md\n+link: {_FRAG}\n*** End Patch\n"
        self.assertTrue(checar_texto(blob))

    def test_linha_menos_sozinha_nao_barra(self):
        blob = (
            "*** Begin Patch\n*** Update File: x.md\n"
            f"-link: {_FRAG}\n"
            "+link: ~/Valt/x.md\n"
            "*** End Patch\n"
        )
        self.assertEqual(checar_texto(blob), [])

    def test_texto_solto_barra(self):
        self.assertTrue(checar_texto(f"ver {_FRAG}\n"))

    def test_bullet_markdown_nao_e_patch(self):
        # Heurística estrutural: "-" de lista NÃO vira patch.
        self.assertTrue(checar_texto(f"- ver {_FRAG}\n"))

    def test_limpo(self):
        self.assertEqual(checar_texto("use ~/Valt/x.md\n"), [])


if __name__ == "__main__":
    unittest.main()
