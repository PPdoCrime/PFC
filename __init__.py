# -*- coding: utf-8 -*-
import os
import sys
import subprocess

# 1. Local da pasta do plugin e da lib
PLUGIN_DIR = os.path.dirname(__file__)
LIB_DIR    = os.path.join(PLUGIN_DIR, 'lib')

# 2. Lista de dependências: (nome no pip, nome do módulo/pasta em lib)
DEPS = [
    ("fuzzywuzzy[speedup]",    "fuzzywuzzy"),
    ("python-Levenshtein",      "Levenshtein"),
    ("rapidfuzz",               "rapidfuzz"),
]

def ensure_libs():
    """Verifica em lib/ e instala o que faltar."""
    if not os.path.isdir(LIB_DIR):
        os.makedirs(LIB_DIR)
    for pip_name, mod_name in DEPS:
        mod_path = os.path.join(LIB_DIR, mod_name)
        if not os.path.isdir(mod_path):
            # instala somente o que não existe
            try:
                subprocess.check_call([
                    sys.executable, "-m", "pip", "install",
                    pip_name,
                    "-t", LIB_DIR
                ])
            except Exception as e:
                # aqui não temos iface ainda, apenas log
                print(f"[SynMap] falha ao instalar {pip_name}: {e}")

# 3. Garante que lib/ exista e esteja no sys.path
ensure_libs()
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

# 4. Registra a factory do QGIS
from .SynMap import SynMapPlugin

def classFactory(iface):
    return SynMapPlugin(iface)
