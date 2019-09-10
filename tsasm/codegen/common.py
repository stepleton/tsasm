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
"""'Common' opcode code generation.

Virtually all architecture-specific handler modules should mix the `get_codegen`
handlers into their collections of code-generating handlers. A module can also
use this module's `encode_str` function if their systems use ASCII.
"""

from typing import Callable, Dict, Text, Tuple

from tsasm.data import all_args_parsed, Context, LABEL_RE, Op, parse_args_if_able, parse_integer, parse_string, ParseOptions, Type


_PARSE_OPTIONS = ParseOptions(
    register_prefix=set(),
    fractional_crements=False,
)


def get_codegen() -> Dict[Text, Callable[[Context, Op], Tuple[Context, Op]]]:
  """Retrieve the dict mapping opcodes to code generators for this machine."""
  generators = {
      'org': _codegen_org,
      '.org': _codegen_org,
  }

  # If your architecture uses a different byte ordering than big-endian, and if
  # you don't need word- or double-word alignment, you should specify different
  # data statement code generators than the ones defined here:
  codegen_db = _gen_codegen_data(1, little_endian=False, align=True)
  codegen_dw = _gen_codegen_data(2, little_endian=False, align=True)
  codegen_dd = _gen_codegen_data(4, little_endian=False, align=True)

  generators['db'] = generators['.db'] = generators['byte'] = codegen_db  # type: ignore
  generators['dw'] = generators['.dw'] = generators['word'] = codegen_dw  # type: ignore
  generators['dd'] = generators['.dd'] = generators['long'] = codegen_dd  # type: ignore
  # mypy is wrong about the above being bad.

  # Index code generators under canonical names.
  return {k.casefold(): v for k, v in generators.items()}


def encode_str(data: Text) -> bytes:
  """Turn the string data in `data` into bytes for this architecture."""
  # We could ask users to define and register custom codecs, but that seems like
  # a real hassle. Just map characters to bytes; raise ValueError for problems.
  return bytes(data, 'ascii')


def _codegen_org(context: Context, op: Op) -> Tuple[Context, Op]:
  """ORG pseudoinstruction: set current output stream position."""
  # Try to parse our one argument. If successful, update our stream position.
  # Otherwise, leaving the op's `todo` unchanged means we'll try again later.
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.ADDRESS))
  if all_args_parsed(op.args):
    op = op._replace(hex='', todo=None)
    context = context._replace(pos=op.args[0].integer)
  return context, op


def _gen_codegen_data(
    element_size: int,
    little_endian: bool,
    align: bool,
    parse_options: ParseOptions = _PARSE_OPTIONS,
) -> Callable[[Context, Op], Tuple[Context, Op]]:
  """'Code generator generator' for data statements: db, dw, dd, etc.

  Args:
    element_size: data quantum, in bytes (e.g. 1 for db, 2 for dw, 4 for dd).
    little_endian: whether numbers should be represented in little-endian
        byte order instead of the proper way.
    align: whether to align generated data to an integer multiple of
        `element_size`.
    parse_options: parsing options for this architecture.

  Returns:
    A code generating handler for a data statement with the specified traits.
  """
  endianity = 'little' if little_endian else 'big'

  def codegen_data(context: Context, op: Op) -> Tuple[Context, Op]:
    # Accumulate hex code here, and track whether we have its final value worked
    # out, or if we're still waiting on labels.
    hexparts = []
    all_hex_ok = True

    # Align the data to match the data quantum, if directed.
    if align:
      if element_size != 1:
        if context.pos is None: raise ValueError(
            'Unresolved labels above this line (or other factors) make it '
            'impossible to know how to align this data statement. Consider '
            "an ORG statement to make this data's memory location explicit.")
        hexparts.append('00' * (context.pos % element_size))

    # Generate data for each arg. Unlike nearly all other statements, we do most
    # of the parsing ourselves.
    for arg in op.args:
      # Is the argument a string?
      if arg.stripped.startswith('"') or arg.stripped.startswith("'"):
        hexparts.append(''.join(
            val.to_bytes(element_size, endianity).hex().upper()
            for val in parse_string(parse_options, context, arg.stripped)))

      # No, it must be a single integer value.
      else:
        # Does the argument look like a label? If so, try to resolve it and take
        # its value. If not, let's parse the argument as an integer value.
        if LABEL_RE.fullmatch(arg.stripped):
          all_hex_ok &= arg.stripped in context.labels
          val = context.labels[arg.stripped] if all_hex_ok else 0
        else:
          val = parse_integer(parse_options, context, arg.stripped)
        # Now encode the value as hex.
        hexparts.append(val.to_bytes(element_size, endianity).hex().upper())

    # Package the hex data from all the args and, if appropriate, mark our job
    # as complete.
    op = op._replace(todo=None if all_hex_ok else op.todo,
                     hex=''.join(hexparts))
    return context.advance(op.hex), op

  return codegen_data
