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
"""Code generation for the IBM PALM processor.

This library isn't meant to be used directly; instead, it supplies code
generators for libraries for specific machines that use PALM processors (and
differ in other details like character sets).

The PALM ("Put All Logic in Microcode") processor was the 16-bit processor in
IBM's 5100, 5110, and 5120 personal computers from the mid-1970s. It features
an orthogonal register file and a compact, almost RISC-like instruction set.
Not bad for 1973. https://en.wikipedia.org/wiki/IBM_PALM_processor

(No hardware multiply and an 8-bit ALU, but you can't have it all.)

These mnemonics are not the IBM originals but instead the ones chosen by
Christian Corti during his reverse-engineering efforts. References:
http://computermuseum.informatik.uni-stuttgart.de/dev/ibm_5110/technik/en/opcodes.html
http://computermuseum.informatik.uni-stuttgart.de/dev/ibm_5110/technik/en/subroutines.html
"""

import logging
from typing import Callable, Dict, Text, Tuple

from tsasm.data import all_args_parsed, Arg, Context, Op, parse_args_if_able, ParseOptions, Type
from tsasm.codegen import common


# All register symbols start with R.
_PARSE_OPTIONS = ParseOptions(
    register_prefix={'r'},
    fractional_crements=False,  # Will set to True for MOVE and CALL, though.
)


def palm_codegen() -> Dict[Text, Callable[[Context, Op], Tuple[Context, Op]]]:
  """Retrieve a dict mapping opcodes to code generators for PALM processors."""
  # Note mix-in of the "common" operations.
  generators = dict({
      'dec2': _gen_codegen_reg_to_reg(0, 0),
      'halt': _codegen_halt,
      'dec': _gen_codegen_reg_to_reg(0, 1),
      'inc': _gen_codegen_reg_to_reg(0, 2),
      'inc2': _gen_codegen_reg_to_reg(0, 3),
      'move': _codegen_move,
      'nop': _codegen_nop,
      'and': _gen_codegen_reg_to_reg(0, 5),
      'or': _gen_codegen_reg_to_reg(0, 6),
      'xor': _gen_codegen_reg_to_reg(0, 7),
      'add': _gen_codegen_add_or_sub('add'),
      'sub': _gen_codegen_add_or_sub('sub'),
      'addh': _gen_codegen_reg_to_reg(0, 0xA),
      'addh2': _gen_codegen_reg_to_reg(0, 0xB),
      'mhl': _gen_codegen_reg_to_reg(0, 0xC),
      'mlh': _gen_codegen_reg_to_reg(0, 0xD),
      'getb': _codegen_getb,
      'getadd': _gen_codegen_dev_to_reg(0),
      'ctrl': _codegen_ctrl,
      'putb': _codegen_putb,
      'movb': _codegen_movb,
      'lbi': _gen_codegen_immed_to_reg(8),
      'clr': _gen_codegen_immed_to_reg(9),
      'set': _gen_codegen_immed_to_reg(0xB),
      'sle': _gen_codegen_reg_to_reg(0xC, 0),
      'slt': _gen_codegen_reg_to_reg(0xC, 1),
      'se': _gen_codegen_reg_to_reg(0xC, 2),
      'sz': _gen_codegen_onereg(0xC, 3, 0),
      'ss': _gen_codegen_onereg(0xC, 4, 0),
      'sbs': _gen_codegen_reg_to_reg(0xC, 5),
      'sbc': _gen_codegen_reg_to_reg(0xC, 6),
      'sbsh': _gen_codegen_reg_to_reg(0xC, 7),
      'sgt': _gen_codegen_reg_to_reg(0xC, 8),
      'sge': _gen_codegen_reg_to_reg(0xC, 9),
      'sne': _gen_codegen_reg_to_reg(0xC, 0xA),
      'snz': _gen_codegen_onereg(0xC, 0xB, 0),
      'sns': _gen_codegen_onereg(0xC, 0xC, 0),
      'snbs': _gen_codegen_reg_to_reg(0xC, 0xD),
      'snbc': _gen_codegen_reg_to_reg(0xC, 0xE),
      'snbsh': _gen_codegen_reg_to_reg(0xC, 0xE),
      'lwi': _codegen_lwi,
      'shr': _gen_codegen_onereg(0xE, 0xC, 1),
      'ror': _gen_codegen_onereg(0xE, 0xD, 1),
      'ror3': _gen_codegen_onereg(0xE, 0xE, 1),
      'swap': _gen_codegen_onereg(0xE, 0xF, 1),
      'stat': _gen_codegen_dev_to_reg(0xE),
      'bra': _codegen_bra,
      'ret': _gen_codegen_onereg(0, 4, 1),  # cheeky
      'jmp': _codegen_jmp,
      'call': _codegen_call,
      'rcall': _codegen_rcall,
  }, **common.get_codegen())

  # Index code generators under canonical names.
  return {k.casefold(): v for k, v in generators.items()}


### Various argument checkers ###


def _regcheck(*args: Arg):
  """Verify registers in all args are valid; raise ValueError if not."""
  for arg in args:
    if not 0 <= arg.integer <= 15: raise ValueError(
        'Invalid register {!r}'.format(arg.stripped))


def _devcheck(*args: Arg):
  """Verify registers in all args are valid; raise ValueError if not."""
  for arg in args:
    if not 0 <= arg.integer <= 15: raise ValueError(
        'Invalid device address {!r} ({})'.format(
            arg.stripped, arg.integer))


def _bytecheck(*args: Arg):
  """Verify value is in the range -128..255; raise ValueError if not."""
  for arg in args:
    if not -128 <= arg.integer <= 255: raise ValueError(
        'Byte literal {!r} ({}) not in range -128..255'.format(
            arg.stripped, arg.integer))


def _regderefcheck(arg: Arg, postcrem_from: int = 0, postcrem_to: int = 0):
  """Verify valid register, and postcrementing in range."""
  if not 0 <= arg.integer <= 15: raise ValueError(
      'Invalid register in dereference {!r}'.format(arg.stripped))
  if arg.precrement != 0: raise ValueError(
      'No IBM PALM instruction supports address pre-(in/de)crementation.')
  if not postcrem_from <= arg.postcrement <= postcrem_to: raise ValueError(
      'Invalid post-(in|de)crement in {!r}; valid range is {}..{}'.format(
          arg.stripped, postcrem_from, postcrem_to))


def _addrcheck(*args: Arg):
  """Verify valid address: none more than 65535."""
  for arg in args:
    if not 0 <= arg.integer <= 65535: raise ValueError(
        'Invalid memory address {!r} (${:X}); valid range is $0..$FFFF'.format(
            arg.stripped, arg.integer))


def _lowwordaddrcheck(*args: Arg):
  for arg in args:
    if arg.integer % 2: raise ValueError(
        'Low word address {!r} (${:X}) is not 16-bit aligned (even)'.format(
            arg.stripped, arg.integer))
    if not 0 <= arg.integer <= 510: raise ValueError(
        'Low word address {!r} (${:X}) is not in range 0..510'.format(
            arg.stripped, arg.integer))


def _jmpdestcheck(*args: Arg):
  """Verify valid address for a jump: must satisfy _addrcheck, must be even."""
  _addrcheck(*args)
  for arg in args:
    if arg.integer % 2: raise ValueError(
        'Invalid jump address {!r} (${:X}); must be 16-bit aligned'.format(
            arg.stripped, arg.integer))


def _callregcheck(arg1: Arg, arg2: Arg):
  """Verify valid registers for a jump: satisfy _addrcheck, be different."""
  _addrcheck(arg1, arg2)
  if arg1.integer == arg2.integer: raise ValueError(
      'Arguments to subroutine call instructions must use different registers')


### Other helpers ###


def _reljmpoffset(context: Context, arg: Arg) -> int:
  """Calculate program counter displacement for a relative jump, if possible."""
  assert context.pos is not None  # Don't call unless we know our position.
  true_displacement = arg.integer - context.pos
  # As the current instruction runs, the address stored in R0 (the program
  # counter) is 2 + the address of the currently-running instruction. This
  # accounts for the bounds and offset calculations done below.
  if not -254 <= true_displacement <= 258: raise ValueError(
      'Invalid relative jump {!r} (${:X}); limits are -254..258'.format(
          arg.stripped, arg.integer))
  offset = true_displacement - 2
  # Beware! ADD and SUB with an immediate argument take a value that's one
  # closer to 0 than the actual value added/subtracted. There's no way to
  # achieve a relative jump to the next instruction (so, a NOP, effectively).
  # Keep this in mind when generating code.
  return offset


def _postcrement_to_modifier(postcrement: float) -> int:
  """Convert a postcrement value (in -4..4) to a modifier value (in 0..8)."""
  return (7, 6, 5, 4, 8, 0, 1, 2, 3)[int(postcrement) + 4]



### Code generators and "generator generators" ###


def _gen_codegen_onereg(
    nybble_1: int,
    nybble_2: int,
    argpos: int,
) -> Callable[[Context, Op], Tuple[Context, Op]]:
  """'Code generator generator' for instructions with just one register arg."""

             # "Position 0"     "Position 1"
  template = ('{:X}{:X}0{:X}', '{:X}0{:X}{:X}')[argpos]

  def codegen_onereg(context: Context, op: Op) -> Tuple[Context, Op]:
    # Both register arguments to this opcode should be parseable.
    op = op._replace(args=parse_args_if_able(
        _PARSE_OPTIONS, context, op, Type.REGISTER))
    if all_args_parsed(op.args):
      _regcheck(*op.args)
      digits = (nybble_1, op.args[0].integer, nybble_2)
      op = op._replace(todo=None, hex=template.format(*digits))
    # We can still update pos whether we've parsed all args or not.
    return context.advance_by_bytes(2), op

  return codegen_onereg


def _gen_codegen_reg_to_reg(
    nybble_1: int,
    nybble_2: int,
) -> Callable[[Context, Op], Tuple[Context, Op]]:
  """'Code generator generator' for register-to-register instructions."""

  def codegen_reg_to_reg(context: Context, op: Op) -> Tuple[Context, Op]:
    # Both register arguments to this opcode should be parseable.
    op = op._replace(args=parse_args_if_able(
        _PARSE_OPTIONS, context, op, Type.REGISTER, Type.REGISTER))
    if all_args_parsed(op.args):
      _regcheck(*op.args)
      digits = (nybble_1, op.args[0].integer, op.args[1].integer, nybble_2)
      op = op._replace(todo=None, hex='{:X}{:X}{:X}{:X}'.format(*digits))
    # We can still update pos whether we've parsed all args or not.
    return context.advance_by_bytes(2), op

  return codegen_reg_to_reg


def _gen_codegen_dev_to_reg(
    nybble: int,
) -> Callable[[Context, Op], Tuple[Context, Op]]:
  """'Code generator generator' for device-to-register instructions."""

  def codegen_dev_to_reg(context: Context, op: Op) -> Tuple[Context, Op]:
    op = op._replace(args=parse_args_if_able(
        _PARSE_OPTIONS, context, op, Type.REGISTER, Type.ADDRESS))
    if all_args_parsed(op.args):
      _regcheck(op.args[0])
      _devcheck(op.args[1])
      digits = (nybble, op.args[0].integer, op.args[1].integer)
      op = op._replace(todo=None, hex='{:X}{:X}{:X}F'.format(*digits))
    # We can still update pos whether we've parsed all args or not.
    return context.advance_by_bytes(2), op

  return codegen_dev_to_reg


def _gen_codegen_immed_to_reg(
    nybble: int,
) -> Callable[[Context, Op], Tuple[Context, Op]]:
  """'Code generator generator' for immediate-to-register instructions."""

  def codegen_immed_to_reg(context: Context, op: Op) -> Tuple[Context, Op]:
    op = op._replace(args=parse_args_if_able(
        _PARSE_OPTIONS, context, op, Type.REGISTER, Type.NUMBER))
    if all_args_parsed(op.args):
      _regcheck(op.args[0])
      _bytecheck(op.args[1])
      digits = (nybble, op.args[0].integer, op.args[1].integer % 256)
      op = op._replace(todo=None, hex='{:X}{:X}{:02X}'.format(*digits))
    # We can still update pos whether we've parsed all args or not.
    return context.advance_by_bytes(2), op

  return codegen_immed_to_reg


def _gen_codegen_add_or_sub(
    add_or_sub: str,
) -> Callable[[Context, Op], Tuple[Context, Op]]:
  """'Code generator generator' for ADD and SUB."""

  def codegen_add_or_sub(context: Context, op: Op) -> Tuple[Context, Op]:
    op = op._replace(args=parse_args_if_able(
        _PARSE_OPTIONS, context, op,
        Type.REGISTER, Type.NUMBER | Type.REGISTER))
    if all_args_parsed(op.args):
      _regcheck(op.args[0])

      # Adding/subtracting an immediate value to/from a register.
      if op.args[1].argtype & Type.NUMBER:
        if not 0 <= op.args[1].integer <= 256: raise ValueError(
          'Literal {!r} not in range 0..256'.format(op.args[1].stripped))
        elif op.args[1].integer == 0:
          assert op.opcode is not None  # mypy...
          logging.warning(
              'Line %d: A #0 literal argument to %s is not supported by the %s '
              'instruction; generating a NOP (MOVE R0, R0) instead',
              op.lineno, op.opcode.upper(), op.opcode.upper())
          op = op._replace(todo=None, hex='0004')
        else:
          digits = (0xA if add_or_sub == 'add' else 0xF,
                    op.args[0].integer, (op.args[1].integer - 1) % 256)
          op = op._replace(todo=None, hex='{:X}{:X}{:02X}'.format(*digits))

      # Adding/subtracting the LSB of one register to/from another register.
      else:
        _regcheck(op.args[1])
        digits = (op.args[0].integer, op.args[1].integer,
                  8 if add_or_sub == 'add' else 9)
        op = op._replace(todo=None, hex='0{:X}{:X}{:X}'.format(*digits))

    # We can still update pos whether we've parsed all args or not.
    return context.advance_by_bytes(2), op

  return codegen_add_or_sub


def _codegen_ctrl(context: Context, op: Op) -> Tuple[Context, Op]:
  """CTRL instruction."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.ADDRESS, Type.NUMBER))
  if all_args_parsed(op.args):
    _devcheck(op.args[0])
    _bytecheck(op.args[1])
    digits = (op.args[0].integer, op.args[1].integer % 256)
    op = op._replace(todo=None, hex='1{:X}{:02X}'.format(*digits))
  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(2), op


def _codegen_putb(context: Context, op: Op) -> Tuple[Context, Op]:
  """PUTB instruction."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.ADDRESS, Type.DEREF_REGISTER))
  if all_args_parsed(op.args):
    _devcheck(op.args[0])
    _regderefcheck(op.args[1], postcrem_from=-4, postcrem_to=4)
    modifier = _postcrement_to_modifier(op.args[1].postcrement)
    digits = (op.args[0].integer, op.args[1].integer, modifier)
    op = op._replace(todo=None, hex='4{:X}{:X}{:X}'.format(*digits))
  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(2), op


def _codegen_getb(context: Context, op: Op) -> Tuple[Context, Op]:
  """GETB instruction."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op,
      Type.REGISTER | Type.DEREF_REGISTER, Type.ADDRESS))
  if all_args_parsed(op.args):
    _devcheck(op.args[1])

    if op.args[0].argtype & Type.REGISTER:
      _regcheck(op.args[0])
      digits_r = (op.args[1].integer, op.args[0].integer)
      op = op._replace(todo=None, hex='0{:X}{:X}E'.format(*digits_r))

    else:  # Type.DEREF_REGISTER
      _regderefcheck(op.args[0], postcrem_from=-4, postcrem_to=4)
      modifier = _postcrement_to_modifier(op.args[0].postcrement)
      digits_d = (op.args[1].integer, op.args[0].integer, modifier)
      op = op._replace(todo=None, hex='E{:X}{:X}{:X}'.format(*digits_d))

  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(2), op


def _codegen_movb(context: Context, op: Op) -> Tuple[Context, Op]:
  """MOVB instruction."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op,
      Type.REGISTER | Type.DEREF_REGISTER, Type.REGISTER | Type.DEREF_REGISTER))
  # Both arguments to this opcode should be parseable.
  if op.args[0].argtype == op.args[1].argtype: raise ValueError(
      'One MOVB argument should be a register, and the other should be a '
      'register dereference')
  if all_args_parsed(op.args):
    nybble, argderef, argreg = ((6, op.args[1], op.args[0])
                                if op.args[0].argtype & Type.REGISTER else
                                (7, op.args[0], op.args[1]))
    _regcheck(argreg)
    _regderefcheck(argderef, postcrem_from=-4, postcrem_to=4)
    modifier = _postcrement_to_modifier(argderef.postcrement)
    digits = (nybble, argreg.integer, argderef.integer, modifier)
    op = op._replace(todo=None, hex='{:X}{:X}{:X}{:X}'.format(*digits))
  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(2), op


def _codegen_move(context: Context, op: Op) -> Tuple[Context, Op]:
  """MOVE instruction."""
  op = op._replace(args=parse_args_if_able(  # Note fractional crements enabled.
      _PARSE_OPTIONS._replace(fractional_crements=True), context, op,
      Type.ADDRESS | Type.REGISTER | Type.DEREF_REGISTER,
      Type.ADDRESS | Type.REGISTER | Type.DEREF_REGISTER))
  if all_args_parsed(op.args):

    if not any(arg.argtype & Type.REGISTER for arg in op.args):
      raise ValueError('At least one argument to MOVE must be a register')

    # This is a move between registers.
    elif op.args[0].argtype == op.args[1].argtype == Type.REGISTER:
      _regcheck(*op.args)
      digits_r = (op.args[0].integer, op.args[1].integer)
      op = op._replace(todo=None, hex='0{:X}{:X}4'.format(*digits_r))

    # This is a move from/to an address found at a specified memory location.
    elif any(arg.argtype == Type.ADDRESS for arg in op.args):
      nybble, argaddr, argreg = ((2, op.args[1], op.args[0])
                                 if op.args[0].argtype & Type.REGISTER else
                                 (3, op.args[0], op.args[1]))
      _regcheck(argreg)
      _lowwordaddrcheck(argaddr)
      digits_a = (nybble, argreg.integer, argaddr.integer // 2)
      op = op._replace(todo=None, hex='{:X}{:X}{:02X}'.format(*digits_a))

    # This is a move from/to an address found in a register.
    else:
      nybble, argderef, argreg = (
          (5, op.args[0], op.args[1])
          if op.args[0].argtype & Type.DEREF_REGISTER else
          (0xD, op.args[1], op.args[0]))
      _regcheck(argreg)
      _regderefcheck(argderef, postcrem_from=-2, postcrem_to=2)  # Words.
      modifier = _postcrement_to_modifier(2 * argderef.postcrement)
      digits_d = (nybble, op.args[1].integer, op.args[0].integer, modifier)
      op = op._replace(todo=None, hex='{:X}{:X}{:X}{:X}'.format(*digits_d))

  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(2), op


def _codegen_halt(context: Context, op: Op) -> Tuple[Context, Op]:
  """HALT pseudoinstruction: DEC2 R0, R0."""
  # This opcode takes no arguments. We still parse to make sure there are none.
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op))
  op = op._replace(todo=None, hex='0000')
  return context.advance(op.hex), op


def _codegen_nop(context: Context, op: Op) -> Tuple[Context, Op]:
  """NOP pseudoinstruction: MOVE R0, R0."""
  # This opcode takes no arguments. We still parse to make sure there are none.
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op))
  op = op._replace(todo=None, hex='0004')
  return context.advance(op.hex), op


def _codegen_lwi(context: Context, op: Op) -> Tuple[Context, Op]:
  """LWI pseudoinstruction: MOVE RX, (RO)+; DW i."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.REGISTER, Type.NUMBER))
  if all_args_parsed(op.args):
    _regcheck(op.args[0])
    if not -32767 <= op.args[1].integer <= 65535: raise ValueError(
        'Halfword literal {} not in range -32768..65535'.format(
            op.args[1].stripped))
    digits = (op.args[0].integer, op.args[1].integer % 65536)
    op = op._replace(todo=None, hex='D{:X}01{:04X}'.format(*digits))
  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(4), op


def _codegen_bra(context: Context, op: Op) -> Tuple[Context, Op]:
  """BRA pseudoinstruction: ADD/SUB R0,#<dist>."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.ADDRESS))
  if all_args_parsed(op.args) and context.pos is not None:
    _jmpdestcheck(op.args[0])
    offset = _reljmpoffset(context, op.args[0])
    if offset == 0:
      logging.warning(
          'Line %d: A BRA of +2 bytes (so, an ordinary PC increment) is not '
          'supported by the usual relative jump techniques; generating a NOP '
          '(MOVE R0, R0) instead', op.lineno)
      op = op._replace(todo=None, hex='0004')
    else:
      digits = (0xA, offset - 1) if offset > 0 else (0xF, -offset - 1)
      op = op._replace(todo=None, hex='{:X}0{:02X}'.format(*digits))

  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(2), op


def _codegen_jmp(context: Context, op: Op) -> Tuple[Context, Op]:
  """JMP pseudoinstruction: several underlying variants."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op,
      Type.ADDRESS | Type.DEREF_REGISTER | Type.DEREF_ADDRESS))
  # Since this pseudoinstruction can produce code of different lengths, we
  # handle updating pos when "not all_args_parsed" in a special way.
  if not all_args_parsed(op.args):
    advance = 4 if op.args[0].argtype & Type.ADDRESS else 2
    return context.advance_by_bytes(advance), op

  # We are branching to an address literal.
  if op.args[0].argtype & Type.ADDRESS:
    _jmpdestcheck(op.args[0])
    op = op._replace(todo=None, hex='D001{:04X}'.format(op.args[0].integer))

  # We are branching to an address stored at a memory location in a register.
  # (To branch to an address inside a register, use RET).
  elif op.args[0].argtype & Type.DEREF_REGISTER:
    _regderefcheck(op.args[0], postcrem_from=0, postcrem_to=0)
    op = op._replace(todo=None, hex='D0{:X}8'.format(op.args[0].integer))

  # We are branching to an address stored at a low memory location.
  else:
    _lowwordaddrcheck(op.args[0])
    op = op._replace(todo=None, hex='20{:02X}'.format(op.args[0].integer // 2))

  return context.advance(op.hex), op


def _codegen_call(context: Context, op: Op) -> Tuple[Context, Op]:
  """CALL pseudoinstruction: several underlying variants."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op,
      Type.ADDRESS | Type.REGISTER | Type.DEREF_REGISTER | Type.DEREF_ADDRESS,
      Type.REGISTER))
  # Since this pseudoinstruction can produce code of different lengths, we
  # handle updating pos when "not all_args_parsed" in a special way.
  if not all_args_parsed(op.args):
    advance = 6 if op.args[0].argtype & Type.ADDRESS else 4
    return context.advance_by_bytes(advance), op

  # We are calling an address literal. Note that there is a way to do this in
  # two halfwords: for that, use the RCALL pseudoinstruction.
  if op.args[0].argtype & Type.ADDRESS:
    _jmpdestcheck(op.args[0])
    _regcheck(op.args[1])
    digits_a = (op.args[1].integer, op.args[1].integer, op.args[0].integer)
    op = op._replace(todo=None, hex='0{:X}03D0{:X}1{:04X}'.format(*digits_a))

  # We are calling an address stored inside a register.
  elif op.args[0].argtype & Type.REGISTER:
    _callregcheck(op.args[0], op.args[1])
    digits_r = (op.args[1].integer, op.args[0].integer)
    op = op._replace(todo=None, hex='0{:X}0300{:X}4'.format(*digits_r))

  # We are calling an address stored at a memory location in a register.
  elif op.args[0].argtype & Type.DEREF_REGISTER:
    _callregcheck(op.args[0], op.args[1])
    _regderefcheck(op.args[0], postcrem_from=-2, postcrem_to=2)  # Words.
    modifier = _postcrement_to_modifier(2 * op.args[0].postcrement)
    digits_d = (op.args[1].integer, op.args[0].integer, modifier)
    op = op._replace(todo=None, hex='0{:X}03D0{:X}{:X}'.format(*digits_d))

  # We are calling an address stored at a low memory location.
  else:
    _regcheck(op.args[1])
    _lowwordaddrcheck(op.args[0])
    assert op.opcode is not None  # mypy...
    if op.args[0].precrement or op.args[0].postcrement: raise ValueError(
        'No (in/de)crementation is allowed for address dereference arguments '
        'to {}'.format(op.opcode.upper()))
    digits = (op.args[1].integer, op.args[0].integer // 2)
    op = op._replace(todo=None, hex='0{:X}0320{:02X}'.format(*digits))

  return context.advance(op.hex), op


def _codegen_rcall(context: Context, op: Op) -> Tuple[Context, Op]:
  """RCALL (R=relocatable) pseudoinstruction: INC2 Rx,R0; BRA <addr>."""
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.ADDRESS, Type.REGISTER))
  assert op.args is not None
  if all_args_parsed(op.args) and context.pos is not None:
    _jmpdestcheck(op.args[0])
    _regcheck(op.args[1])
    offset = _reljmpoffset(context, op.args[0])
    if offset == 0:
      logging.warning(
          'Line %d: A +2-byte RCALL (so, an ordinary PC increment) is not '
          'supported by the usual relative jump techniques; generating a NOP '
          '(MOVE R0, R0) instead', op.lineno)
      op = op._replace(todo=None, hex='0{:X}030004'.format(op.args[1].integer))
    else:
      digits = ((op.args[1].integer, 0xA, offset - 1)
                if offset > 0 else
                (op.args[1].integer, 0xF, -offset - 1))
      op = op._replace(todo=None, hex='0{:X}03{:X}0{:02X}'.format(*digits))

  # We can still update pos whether we've parsed all args or not.
  return context.advance_by_bytes(4), op
