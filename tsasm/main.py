#!/usr/bin/python3
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
"""The tsasm pure-python assembler.

In this initial version: no macro facilities.
"""

import argparse
import importlib
import itertools
import logging
import re
import sys

from typing import BinaryIO, Callable, Dict, List, Optional, Set, Text, TextIO, Tuple

from tsasm.data import Arg, Context, LABEL_RE, Op


# Regexes for lexical analysis.
_RE_STR_APOSTROPHE = r"'(?:\\.|[^\\'])*?'"  # Matches '-delimited strings.
_RE_STR_QUOTEMARKS = r'"(?:\\.|[^\\"])*?"'  # Matches "-delimited strings.
# Matches all text preceding a ; character not in a string.
_RE_CODE = re.compile(r'(?:[^\'";]' + '|' +
                      _RE_STR_APOSTROPHE + '|' +
                      _RE_STR_QUOTEMARKS + ')*')
# Matches individual tokens, including strings, with escaping of string
# delimiters within strings.
_RE_TOKEN = re.compile(r'(?:[^\'"\s,]' + '|' +
                       _RE_STR_APOSTROPHE + '|' +
                       _RE_STR_QUOTEMARKS + ')+')


def _define_flags() -> argparse.ArgumentParser:
  """Command-line options and flags."""
  parser = argparse.ArgumentParser(description='The tsasm assembler')

  parser.add_argument(
      'input_file', nargs='?', type=argparse.FileType('r'),
      default=sys.stdin, help='Input file with assembly code')
  parser.add_argument(
      'output_file', nargs='?', type=argparse.FileType('wb'), default='a.out',
      help='Output file receiving **binary** output (a.out the default)')

  parser.add_argument(
      '--listing', nargs='?', type=argparse.FileType('w'),
      help='Write an listing of assembled code to this file')
  parser.add_argument(
      '--arch', nargs='?', type=str, default='common',
      help=('Assemble code for this architecture; not strictly necessary---you '
            'can use the ARCH pseudo-opcode instead'))

  return parser


class Error(Exception):
  """For errors encountered during assembly.

  Architecture-specific libraries in codegen/ need not use this type; instead,
  they should simply raise ValueError with a string describing the problem.
  """

  def __init__(self, lineno: int, line: Text, why: Text) -> None:
    super().__init__()
    self.lineno, self.line, self.why = lineno, line, why

  def __str__(self) -> str:
    return ('### Fatal error on line {}:\n###   {}\n### {}'.format(
        self.lineno, self.line, '\n### '.join(self.why.split('\n'))))


def read_source(source_input: TextIO) -> Tuple[List[Op], Tuple[Text, ...]]:
  """Load and preprocess source code from the input. Some lexing, too.

  Args:
    source_input: Source code input. All lines of this input will be consumed.

  Returns:
    A 2-tuple with these entries:
    [0]: Source-code lines loaded from the file, as Op objects. Only `lineno`,
         `line`, `labels`, `tokens`, and `todo` fields are specified in these
         Ops; the `todo` field directs that the line's next step lexing.
    [1]: Every line of the original file, with newlines removed.
  """
  ops: List[Op] = []                    # Accumulates Op objects.
  lines: List[Text] = []                # Accumulates lines of source code text.
  current_labels: Set[Text] = set()     # Labels to refer to the next code line.
  claimed_labels: Dict[Text, int] = {}  # Labels already set, and on which line.

  for lineno, line in enumerate(source_input):
    # Initial processing: strip newlines.
    line = line.rstrip('\r\n')
    lines.append(line)
    # Strip comments.
    match = _RE_CODE.match(line)  # Guaranteed to match at least once.
    assert match is not None      # Although mypy doesn't believe me.
    code = match[0]
    # Tokenise the code.
    tokens = tuple(_RE_TOKEN.findall(code))

    # Is there a label? If so, check validity and uniqueness. If checks pass,
    # add the label to current and claimed labels.
    if tokens and tokens[0].endswith(':') and LABEL_RE.match(tokens[0][:-1]):
      label, tokens = tokens[0][:-1], tokens[1:]
      if label in claimed_labels: raise Error(
          lineno, line, 'The label {} was already used on line {}'.format(
              label, claimed_labels[label]))
      current_labels.add(label)
      claimed_labels[label] = lineno

    # This rest of this line (if it exists) is apparently intended to be a line
    # of source code. Save it along with any labels assocated with the line,
    # and indicate that the next step for the line is lexing.
    if tokens:
      ops.append(Op(
          lineno=lineno,
          line=line,
          tokens=tokens,
          labels=tuple(sorted(current_labels)),
          todo=asmpass_lexer))
      current_labels.clear()

  return ops, tuple(lines)


def asmpass_lexer(context: Context, op: Op) -> Tuple[Context, Op]:
  """Perform lexical analysis on a source code line.

  Ultimately, this means breaking the line up into an opcode and its arguments.
  This sets the `opcode` and `args` fields of `op`.

  Args:
    context: current assembler context.
    op: source code line data structure.

  Returns:
    context: updated assembler context.
    op: updated source code line data structure.
  """
  # Obtain opcode and arguments. At least the opcode is guaranteed to exist.
  opcode, etcetera = op.tokens[0], op.tokens[1:]
  opcode = opcode.casefold()  # Canonicalise opcode.
  args = tuple(Arg(stripped=a.strip()) for a in etcetera)
  # Update op with opcode and args, then trigger code generation in the next
  # pass. Argument parsing occurs during code generation, allowing for symbols
  # to be bound as late as possible.
  op = op._replace(opcode=opcode, args=args, todo=asmpass_codegen)
  return context, op


def asmpass_codegen(context: Context, op: Op) -> Tuple[Context, Op]:
  """Attempts to generate binary code from a partially-parsed source code line.

  Except for a few "built-in" opcodes, this function defers code-generation
  to special opcode-specific handlers found in context.codegen. Intuitively,
  these handlers should "do their best" to complete the information in `op` and
  advance the current output position (`context.pos`). If they can do both,
  they should return an updated `op` where `op.todo` is None. In all other
  cases, `op.todo` should be set to this function for another try.

  Args:
    context: current assembler context.
    op: source code line data structure.

  Returns:
    context: updated assembler context.
    op: updated source code line data structure.
  """
  # If there are any labels, attempt to bind them now before an `org` statement
  # sends us packing to another binary location.
  for label in op.labels:
    context = context.bind_label(label)

  # Handle "built-in" opcodes.
  assert op.args is not None
  if op.opcode in ('cpu', '.cpu', 'arch', '.arch'):
    if len(op.args) != 1: raise ValueError(
        'The {} pseudo-opcode takes one argument'.format(op.opcode.upper()))
    op = op._replace(todo=None)
    context = _switch_arch(op.lineno, op.line, context, op.args[0].stripped)

  # Hand over remaining processing to "architecture specific" code generators.
  else:
    if op.opcode not in context.codegen: raise Error(
        op.lineno, op.line,
        'Opcode "{}" not recognised for architecture {}'.format(
            op.opcode, context.arch))
    context, op = context.codegen[op.opcode](context, op)

  # If we haven't bound all the labels associated with this line of code, then
  # we've got to try generating this line of code again, no matter what the
  # opcode's code-generating handler thinks about it.
  if not all(label in context.labels for label in op.labels):
    op = op._replace(todo=asmpass_codegen)

  # If we haven't got an output location for the hex data generated from this
  # line of code, then we force another pass in that case, too.
  if context.pos is None:
    op = op._replace(todo=asmpass_codegen)

  return context, op


def assemble(
    context: Context,
    input_file: TextIO,
    output_file: BinaryIO,
    listing_file: Optional[TextIO],
):
  """Assemble source code from a file.

  Args:
    context: an assembler context.
    input_file: handle for file containing input source code.
    output_file: handle for file receiving binary output.
    listing_file: optional handle for file receiving a text listing.

  Raises:
    Error: if any error is encountered.
  """
  # Load source code from the input.
  ops, lines = read_source(input_file)
  if not ops: raise Error(-1, '<EOF>', 'No code to compile in the input?')

  # Track where each op will commit its hex data to RAM. Entries are None when
  # we don't know that yet.
  addrs: List[Optional[int]] = [None] * len(ops)

  # Keep making passes through all of the ops until the number of pending
  # invocations of `asmpass_codegen` stops changing.
  num_ops_with_codegen_todos = None
  for pass_count in itertools.count(start=1):

    # At the beginning of the pass, reset the current output position to 0.
    context = context._replace(pos=0)

    # Perform a pass through the code. When catching errors, ValueErrors are
    # "normal" errors owing to bugs in user code; other types are "internal"
    # errors that are likely our fault.
    for i in range(len(ops)):
      # If the position of this op has already been calculated, that value is
      # authoritative. Otherwise, if we have new knowledge of this position,
      # and we're at or after code generation, save it.
      if addrs[i] is not None:
        context = context._replace(pos=addrs[i])
      elif context.pos is not None and ops[i].todo is not asmpass_lexer:
        addrs[i] = context.pos

      # If this op has a `todo`, execute it and apply some checks.
      if ops[i].todo is not None:
        try:
          context, ops[i] = ops[i].todo(context, ops[i])
          if ops[i].hex and len(ops[i].hex) % 2: raise Error(
              ops[i].lineno, ops[i].line, 'Extra nybble in generated hex.')
        except ValueError as error:
          raise Error(ops[i].lineno, ops[i].line, str(error))
        except Exception as error:
          raise Error(ops[i].lineno, ops[i].line,
                      'Internal error, sorry!\n  {}'.format(error))

    # With the pass complete, see if it's time to stop.
    ops_with_codegen_todos = tuple(
        op for op in ops if op.todo == asmpass_codegen)
    if num_ops_with_codegen_todos == len(ops_with_codegen_todos): break
    num_ops_with_codegen_todos = len(ops_with_codegen_todos)

  # See if compilation was successful.
  if ops_with_codegen_todos: raise Error(
      ops[-1].lineno + 1, '<EOF>',
      'After {} passes, {} statements still have unresolved labels or other '
      'issues preventing full assembly. These statements are:\n'
      '  {}\n'.format(pass_count, len(ops_with_codegen_todos),
                      '\n  '.join('{:>5}: {}'.format(op.lineno, op.line)
                                  for op in ops_with_codegen_todos)))

  # Construct a mapping from memory addresses to ops whose binary data will
  # start at those addresses. Complain if multiple ops that actually generate
  # binary data attempt to start in the same location.
  addr_to_op: Dict[int, Op] = {}
  for addr, op in zip(addrs, ops):
    maybe_old_op = addr_to_op.setdefault(addr, op)
    if maybe_old_op is not op and maybe_old_op.hex and op.hex: logging.warning(
        'At memory location $%X: replacing previously-generated code.\n'
        '   old - %5d: %s\n   new - %5d: %s',
        addr, maybe_old_op.lineno, maybe_old_op.line, op.lineno, op.line)

  # Write binary output.
  _emit_binary(output_file, addr_to_op)

  # Write listing.
  if listing_file: _emit_listing(listing_file, lines, addr_to_op)


def _switch_arch(
    lineno: int, line: Text, context: Context, arch: Text,) -> Context:
  """Switch the architecture we're generating code for."""
  try:
    module = importlib.import_module('.' + arch, 'tsasm.codegen')
    context = context._replace(
        arch=arch,
        codegen=getattr(module, 'get_codegen')(),
        encode_str=getattr(module, 'encode_str'))
  except (ModuleNotFoundError, AttributeError):
    raise Error(lineno, line,
                'Failed to load a code-generation library for architecture '
                '{!r}'.format(arch))
  return context


def _emit_binary(output_file: BinaryIO, addr_to_op: Dict[int, Op]):
  """Write binary data in an address-to-Op map to an output file."""
  pos = 0  # Amount of binary data written so far.
  for addr, op in sorted(addr_to_op.items()):
    # Add $00 padding to get from pos to addr, or, if "negative padding" would
    # be required, warn the user and refuse to write output for this op.
    if addr > pos:
      output_file.write(b'\0' * (addr - pos))
      pos = addr
    elif addr < pos:
      logging.warning(
          'Not writing the following source code line to the binary output:\n'
          '   %5d: %s\nsince it wishes to be written at memory location $%X, '
          'and we have already\nwritten $%X bytes to the output already.',
          op.lineno, op.line, addr, pos)
    # Write hex data for this line to the output.
    if op.hex is not None:
      data = bytes.fromhex(op.hex)
      output_file.write(data)
      pos += len(data)


def _emit_listing(
    listing_file: TextIO,
    lines: Tuple[Text, ...],
    addr_to_op: Dict[int, Op],
):
  """Emit lines of the input source file annotated with assembly results."""
  # Construct a map from source line to (address, Op) tuple.
  lineno_to_addr_op = {op.lineno: (addr, op) for addr, op in addr_to_op.items()}

  # Determine what the longest hex string is in all of the ops, and from this,
  # how much of the printed line to devote to hex data.
  max_hex_len = max(len(op.hex or '') for op in addr_to_op.values())
  # Okay, but if someone has a really long data statement, or if some
  # architecture has really long instructions, we need to wrap the hex over
  # multiple lines, so cap max_hex_len at 16.
  max_hex_len = min(max_hex_len, 16)
  hexwidth = max_hex_len + (max_hex_len - 1) // 4

  # Helper: create space-separated hex display, e.g. 0123 ABCD
  make_hexdata = lambda h: ' '.join(
      h[i:i+4] for i in range(0, len(h), 4)).upper()

  # Create listing, line by line.
  addr = 0
  for lineno, line in enumerate(lines):
    # Print up to 16 digits of hex data, plus the line of code itself.
    op_hex_rest = hexdata = ''
    if lineno in lineno_to_addr_op:
      addr, op = lineno_to_addr_op[lineno]
      # For marshaling hex data to print, up to the first 16 hex digits can fit.
      op_hex_rest = (op.hex or '').upper()
      op_hex_first, op_hex_rest = op_hex_rest[:16], op_hex_rest[16:]
      hexdata = make_hexdata(op_hex_first)
    print(f'{lineno:5}/{addr:>8X} : {hexdata:{hexwidth}}  {line}',
          file=listing_file)

    # Print any hex data that remains in 16-digit chunks.
    while op_hex_rest:
      addr += 8
      op_hex_first, op_hex_rest = op_hex_rest[:16], op_hex_rest[16:]
      hexdata = make_hexdata(op_hex_first)
      print(f'{lineno:5}/{addr:>8X} : {hexdata:{hexwidth}}', file=listing_file)


def main(FLAGS: argparse.Namespace):
  """Main function."""
  # Create assembler context; load code generators for the chosen architecture.
  context = _switch_arch(
      -1, '--arch={}'.format(FLAGS.arch),
      Context(arch='', codegen={}, encode_str=lambda s: bytes(s, 'ascii')),
      FLAGS.arch)
  # Run the assembler.
  try:
    assemble(context, FLAGS.input_file, FLAGS.output_file, FLAGS.listing)
  except Error as error:
    print(error, file=sys.stderr)


if __name__ == '__main__':
  flags = _define_flags()
  FLAGS = flags.parse_args()
  main(FLAGS)
