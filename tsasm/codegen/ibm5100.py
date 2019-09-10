# coding: utf8
# Copyright 2019 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Code generation for the IBM 5100 portable computer.

The IBM 5100 uses a processor called PALM. For more information about PALM
and the actual code-generating code, see `_ibmpalm.py`.

This library adds 5100-specific character-handling. The 5100 uses a different
character set to those found in other PALM-based machines like the 5110 and
5120.
"""

import logging
from typing import Callable, Dict, Text, Tuple

from tsasm.data import all_args_parsed, Arg, Context, Op, parse_args_if_able, ParseOptions, Type
from tsasm.codegen import _ibmpalm, common


# A best effort to recreate the IBM 5100 character set, as shown on page 6-24
# of the October 1979 Maintenance Information Manual. Not all of the characters
# there have ready analogues in Unicode. Some notes:
#
# 1. All underscored characters ($80-$FF) are absent. The APL FUNCTIONAL *
#    characters do include some UNDERBAR letters, but these are likely
#    semantically distinct from... whatever IBM intended the underscored
#    characters to be used for. Using the Unicode combining underscore (U+0332,
#    COMBINING LOW LINE) seems like asking for trouble. You'll just have to use
#    integers to represent those characters in your code.
#
# 2. Codepoint $62 is a character that Unicode might call APL FUNCTIONAL
#    SYMBOL DEL TILDE if it existed---you can recreate it with combining
#    characters as ̴∆, but to avoid complications from using combining
#    characters, specify ᵭ instead.
#
# 3. Codepoint $75 is something that resembles a capital O or U overstruck
#    with a capital T. Until a superior Unicode substitute is found, specify Ⓣ
#    instead.
#
# 4. Codepoint $79 resembles a capital P with a subscript capital T. Until a
#    superior Unicode substitute is found, specify the "prescription symbol" ℞
#    instead.
#
# Finally, beware that some characters in this collection may not be the ones
# you think they are. For example, '∨' is not 'v'.
_CHARACTER_SET = (
    r""" ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/+×←[],.⍺⊥∩⌊∈_∇∆⍳∘'⎕∣⊤○⋆?⍴⌈"""
    r"""∼↓∪⍵⊃↑⊂∧¨¯<≤=≥>≠∨\-÷→();:⌽⊖⍉⍟⌿⍀⍞!⍫ᵭ⍟⌹⌶⍝⍲⍱⍒⍋⍕⍎¬"&@#$%ÄⓉÖÜÅÆ℞Ñ£ÇÕÃ"""
)


# For now, the code generator is just the PALM code generator. We have no
# 5100-specific opcodes or pseudo-opcodes yet.
get_codegen = _ibmpalm.palm_codegen


def encode_str(data: Text) -> bytes:
  """Turn the string data in `data` into bytes for the IBM 5100."""
  try:
    return bytes(_CHARACTER_SET.index(c) for c in data)
  except ValueError:
    missing = ''.join(c for c in data if c not in _CHARACTER_SET)
    raise ValueError('The IBM 5100 character set is missing some of the '
                     'characters in {!r}: ->{}<-'.format(data, missing))

