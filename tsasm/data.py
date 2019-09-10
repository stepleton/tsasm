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
"""Data structures and some helper functions for tsasm."""

import enum
import re

from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Text, Tuple


# A regular expression matching valid label strings. There's no better place to
# keep this than here, regrettably.
LABEL_RE = re.compile(r'[_a-zA-Z][\w_]*')


class Type(enum.Flag):
  """Type information for an argument in a line of source code.

  See comments on individual flag definitions for details.
  """
  # The argtype values in Arg may take at most one of these flags, but in other
  # settings (e.g. specifying acceptable Arg types), they may be combined.
  NUMBER = enum.auto()          # A raw number
  ADDRESS = enum.auto()         # A location in memory
  REGISTER = enum.auto()        # A register
  DEREF_ADDRESS = enum.auto()   # Value pointed to by an address in memory
  DEREF_REGISTER = enum.auto()  # Value pointed to by an address in a register

  # Any arg that is not fully parsed (e.g. due to an unbound label) will also
  # have this flag set. In cases where the arg is completely ambiguous, only
  # this flag will be set.
  UNPARSED = enum.auto()        # An argument that hasn't been parsed yet


class ParseOptions(NamedTuple):
  """Options passed along to the argument parser.

  Attributes:
    register_prefix: a set of strings listing valid register prefixes for the
        current architecture. In some cases the prefix is the entire register
        name; for civilised architectures, we might expect the most-used
        prefixes to be followed by a number. Prefix-matching is always
        insensitive to the case choices of the user, but for this option, you,
        the implementer, should always use lowercase! For 68000 we might say
        {'d', 'a', 'sp', 'pc', 'sr', 'ccr'}.
    fractional_crements: whether to permit fractional address incrementation and
        decrementation during dereferences. The "half-increment" symbol is '
        (single quote), and the "half-decrement" symbol is ~ (tilde).
  """
  register_prefix: Set[Text]
  fractional_crements: bool


class Arg(NamedTuple):
  """Represents one argument in a line of source code.

  Attributes:
    stripped: Orignal text representation of the argument with leading and
        trailing whitespace removed.
    argtype: The type of this argument.
    integer: Integer associated with this argument. The meaning of this integer
        depends on the argtype: for example, for an ADDRESS argument, it will be
        the memory address.
    register_prefix: For REGISTER and DEREF_REGISTER arguments, this is the
        register prefix in the register specification (e.g. 'A' in 'A3').
    precrement: For DEREF_* arguments, by how many units
        (architecture-dependent) the dereferenced address should be incremented
        or decremented prior to dereferencing.
    postcrement: For DEREF_* arguments, by how many units
        (architecture-dependent) the dereferenced address should be incremented
        or decremented after dereferencing.
  """
  stripped:        Text
  argtype:         Type = Type.UNPARSED
  integer:         int = 0
  register_prefix: Text = ''
  precrement:      float = 0
  postcrement:     float = 0

  def parse(self, options: ParseOptions, context: 'Context', op: 'Op') -> 'Arg':
    # Don't repeat work that's already been completed.
    if not self.argtype & Type.UNPARSED: return self
    # Otherwise, try and parse ourselves multiple ways.
    try:
      return _attempt_several_parses([
          ('as a dereference', _parse_deref),
          ('   as a register', _parse_register),
          ('     as a number', _parse_number),
          ('   as an address', _parse_address)],
          options, context, self.stripped)
    except ValueError as e:
      raise ValueError('While parsing {!r} on line {}:\n{}'.format(
          op.opcode.upper(), op.lineno, e))


class Op(NamedTuple):
  """Represents a line of source code and its compiled result.

  Attributes:
    lineno: Source line number of this line. Use only for messages and warnings.
    line: Raw input text data for this source code line.
    tokens: The contents of line split into tokens.
    labels: Any labels associated with this source code line.
    opcode: Opcode extracted from this source code line, or None if the line
        has not been fully lexed yet.
    args: Arguments extracted from this source code line, or the empty tuple if
        the line has not been fully lexed yet.
    hex: Generated hex data for this source code line, or None if it hasn't
        been generated yet.
    todo: A callable that we should evaluate to advance the processing of this
        source code line.
  """
  lineno:   int
  line:     Text
  tokens:   Tuple[Text, ...] = ()
  labels:   Tuple[Text, ...] = ()
  opcode:   Optional[Text] = None
  args:     Tuple[Arg, ...] = ()
  hex:      Optional[Text] = None
  todo:     Optional[Callable[['Context', 'Op'], Tuple['Context', 'Op']]] = None


class Context(NamedTuple):
  """Represents the assembler's internal state information.

  Attributes:
    arch: Target architecture for the assembler.
    codegen: Opcode-indexed code generation helper functions.
    encode_str: A helper that converts string data to bytes.
    labels: Labels referring to memory locations.
    pos: Current position in the binary output.
  """
  arch:       Text
  codegen:    Dict[Text, Callable[['Context', Op], Tuple['Context', Op]]]
  encode_str: Callable[[Text], bytes]
  labels:     Dict[Text, int] = {}
  pos:        Optional[int] = 0

  # In all methods below, if we don't know where we are in the binary output
  # (due to unresolved label bindings, for example), we can't update our
  # position in the output or bind any label yet, and so we make no changes.

  def advance(self, hexdata: Optional[Text]) -> 'Context':
    """Advance binary output position to accommodate `hexdata`."""
    assert hexdata is not None  # hexdata is only optional to put mypy at ease.
    if self.pos is None: return self
    return self._replace(pos=self.pos + len(hexdata) // 2)

  def advance_by_bytes(self, num_bytes: int) -> 'Context':
    """Advance binary output position by `num_bytes`."""
    if self.pos is None: return self
    return self._replace(pos=self.pos + num_bytes)

  def bind_label(self, label: Text) -> 'Context':
    """Bind `label` to the current binary output position."""
    if self.pos is not None: self.labels[label] = self.pos
    return self


def parse_args_if_able(
    options: ParseOptions,
    context: Context,
    op: Op,
    *argtypes: Type
) -> Tuple[Arg, ...]:
  """Attempt to parse all arguments in `op`.

  Args:
    options: argument parsing options.
    context: current assembler context.
    op: op whose arguments we attempt to parse.
    *argtypes: argument type specifications; we will attempt to parse as many
        args as there are specified types, and an arg is only valid if its type
        appears as one of the flags in its corresponding specification.

  Raises:
    ValueError: op has a different number of arguments than the number of
        argument type specifications provided, or the type of an arg does not
        match the type(s) specified in its corresponding argtypes entry.
  """
  # Check for the correct number of arguments.
  assert op.opcode is not None
  if len(op.args) != len(argtypes): raise ValueError(
      '{} takes exactly {} argument{}'.format(
          op.opcode.upper(), len(argtypes), '' if len(argtypes) == 1 else 's'))

  # Attempt to parse arguments.
  args = tuple(arg.parse(options, context, op) for arg in op.args)

  # Check that arguments are appropriate types.
  for i, (arg, argtype) in enumerate(zip(args, argtypes)):
    if not arg.argtype & argtype: raise ValueError(
        'Argument {} to {} must have type {!r}, not {!r}'.format(
            i + 1, op.opcode.upper(), argtype, arg.argtype))

  return args


def all_args_parsed(args: Iterable[Arg]) -> bool:
  return not any(arg.argtype & Type.UNPARSED for arg in args)


def parse_string(options: ParseOptions, context: Context, t: Text) -> bytes:
  r"""Parse a delimited string.

  Parses a string enclosed in single-quote (') or double-quote (") delimiters.
  Escaping with '\' is permitted; any character immediately following is
  included in the string verbatim. The `context.encode_str` function will be
  used to convert the loaded string into architecture-specific bytes.

  Args:
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as a delimited string.

  Returns:
    The parsed string converted to bytes.

  Raises:
    ValueError: the text data in `t` was not parseable as a delimited string.
  """
  # We must start and end with the same two delimiters.
  if len(t) < 2 or t[0] != t[-1]: raise ValueError(
      'Could not parse {!r} as a delimited string.')

  def unescape(chars):
    r"""Delete the 1st, 3rd, 5th, ... '\' character in `chars` (an iterator)."""
    try:
      while True:
        while True:
          char = next(chars)
          if char == '\\': break
          yield char
        yield next(chars)
    except StopIteration:
      return

  # Perform our primitive unescaping, then convert to bytes and return.
  t = ''.join(unescape(iter(t[1:-1])))
  return context.encode_str(t)


def parse_integer(options: ParseOptions, context: Context, t: Text) -> int:
  """Parse an integer.

  In addition to any form that Python's `int(sometext, base=0)` can parse (you
  know, things like 0x123), integer literals can take these familiar forms:
    - '$13AC': a hexadecimal non-negative integer
    - '-3B2h': a hexadecimal integer
    - '1010b': a binary integer
    - '1755o': an octal integer
    - '0644q': also an octal integer
    - '-123d': a decimal integer.
    -     'c': a single-character string.
    -     "c": (same)

  Args:
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as an integer.

  Returns:
    The parsed integer.

  Raises:
    ValueError: the text data in `t` was not parseable as an integer.
  """
  if not t: raise ValueError(
      "Attempted to parse the empty string '' as an integer")
  original = t  # Save the original text.

  # If this looks like a single-character string, attempt to parse it, then use
  # its byte value.
  if t[0] in '"\'':
    b = parse_string(options, context, t)
    if len(b) != 1: raise ValueError(
        'Only one-character strings may be used as integer literals')
    return b[0]

  # Canonicalise "letters" in the text to lowercase and strip whitespace.
  t = t.lower().strip()

  # Convert prefix notation to postfix notation.
  if t.startswith('$'): t = t[1:] + 'h'

  # Save unary + or - for later.
  if t[0] in '+-':
    sign = t[0]
    t = t[1:]
  else:
    sign = ''

  # Convert postfix notation to Python notation.
  if t.endswith('h'): t = '0x' + t[:-1]
  elif t.endswith('b'): t = '0b' + t[:-1]
  elif t.endswith('o'): t = '0o' + t[:-1]
  elif t.endswith('q'): t = '0o' + t[:-1]
  elif t.endswith('d'): t = t[:-1]

  # Restore sign and attempt to parse.
  try:
    return int(sign + t, base=0)
  except ValueError:
    raise ValueError('Malformed numeric text {!r}'.format(original))


def _parse_number(options: ParseOptions, context: Context, t: Text) -> Arg:
  """Parse a numerical value, or retrieve it from labels.

  Args:
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as a number.

  Returns:
    An Arg whose argtype is NUMBER, or if the text appears to specify a label
    but the label is not bound, an Arg whose argtype is UNPARSED.

  Raises:
    ValueError: the text data in `t` was not parseable as a number.
  """
  if not t: raise ValueError(
      "Attempted to parse the empty string '' as a number")
  original = t  # Save the original text.

  # Canonicalise by stripping.
  t = t.strip()

  def complain():
    raise ValueError('Malformed numerical value {!r}'.format(original))

  # The first character for a numerical value should be '#'.
  if t[0] != '#': complain()
  tonumber = t[1:]

  # Try first parsing the value as an integer.
  try:
    return Arg(stripped=tonumber, argtype=Type.NUMBER,
               integer=parse_integer(options, context, tonumber))
  except ValueError:
    pass

  # Try parsing it now as a label.
  if not LABEL_RE.fullmatch(tonumber): complain()
  try:
    return Arg(stripped=t, argtype=Type.NUMBER,
               integer=context.labels[tonumber])
  except KeyError:
    return Arg(stripped=t, argtype=Type.NUMBER | Type.UNPARSED)


def _parse_address(options: ParseOptions, context: Context, t: Text) -> Arg:
  """Parse a memory address, or retrieve it from labels.

  Args:
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as an address.

  Returns:
    An Arg whose argtype is ADDRESS, or if the text appears to specify a label
    but the label is not bound, an Arg whose argtype is UNPARSED.

  Raises:
    ValueError: the text data in `t` was not parseable as an address.
  """
  if not t: raise ValueError(
      "Attempted to parse the empty string '' as an address")
  original = t  # Save the original text.

  # Canonicalise by stripping.
  t = t.strip()

  def complain():
    raise ValueError('Malformed address {!r}'.format(original))

  # Try first parsing the value as an integer.
  try:
    return Arg(stripped=t, argtype=Type.ADDRESS,
               integer=parse_integer(options, context, t))
  except ValueError:
    pass

  # Try parsing it now as a label.
  if not LABEL_RE.fullmatch(t): complain()
  try:
    return Arg(stripped=t, argtype=Type.ADDRESS, integer=context.labels[t])
  except KeyError:
    return Arg(stripped=t, argtype=Type.ADDRESS | Type.UNPARSED)


def _parse_register(options: ParseOptions, context: Context, t: Text) -> Arg:
  """Parse a register specification.

  Args:
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as a register specification.

  Returns:
    An Arg whose argtype is REGISTER.

  Raises:
    ValueError: the text data in `t` was not parseable as a register spec.
  """
  if not t: raise ValueError(
      "Attempted to parse the empty string '' as a register specification")
  original = t  # Save the original text.

  # Canonicalise to stripped lowercase.
  t = t.lower().strip()

  # Try to capture the longest-possible register prefix.
  for prefix in sorted(options.register_prefix, key=len, reverse=True):
    if t.startswith(prefix):
      regnum_text = t[len(prefix):]
      break
  else: raise ValueError(
      'Register specification {!r} has an unknown prefix.'.format(original))

  # Obtain the register number, if one is specified. Otherwise, -1 is used.
  regnum = parse_integer(options, context, regnum_text) if regnum_text else -1

  return Arg(stripped=t, argtype=Type.REGISTER,
             integer=regnum, register_prefix=prefix)


def _parse_deref(
    options: ParseOptions, context: Context, t: Text) -> Arg:
  """Parse an argument that dereferences and maybe "crements" an address.

  The address to dereference may be stored in a register or a memory location.

  Args:
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as a dereference.

  Returns:
    An Arg whose argtype is DEREF_ADDRESS or DEREF_REGISTER, or if the address
    appears to be stored in a memory address referred to by a label that is not
    bound to a value, an Arg whose argtype is UNPARSED.

  Raises:
    ValueError: the text data in `t` was not parseable as a dereference.
  """
  if not t: raise ValueError(
      "Attempted to parse the empty string '' as a dereference")
  original = t  # Save the original text.

  # Canonicalise by stripping.
  stripped = t.strip()
  t = stripped

  def complain():
    raise ValueError('Malformed dereference {!r}'.format(original))

  # Here are the increments of "crementation". We can have fractional (half)
  # increments to denote things like advancing by single bytes in operations
  # that consume whole halfwords.
  crements: Dict[Text, float] = {'-': -1, '+': 1}
  if options.fractional_crements: crements.update({'~': -0.5, "'": 0.5})

  # Count "precrementation".
  precrement: float = 0
  while t[0] in crements:
    precrement += crements[t[0]]
    t = t[1:]
    if not t: complain()

  # The next character should be '('.
  if t[0] != '(': complain()
  t = t[1:]

  # Identify what we're dereferencing.
  try:
    toderef_text, t = t.split(')')
  except ValueError:
    complain()

  toderef = _attempt_several_parses(
      [('  as a register', _parse_register),
       ('  as an address', _parse_address)],
      options, context, toderef_text)

  # Count "postcrementation".
  postcrement: float = 0
  while t and t[0] in '+-':
    postcrement += crements[t[0]]
    t = t[1:]

  # That should account for all the text.
  if t: complain()

  # Construct the return type.
  argtype = (Type.DEREF_REGISTER if toderef.argtype & Type.REGISTER
             else Type.DEREF_ADDRESS)
  argtype |= (toderef.argtype & Type.UNPARSED)
  return Arg(argtype=argtype, stripped=stripped, integer=toderef.integer,
             precrement=precrement, postcrement=postcrement)


def _attempt_several_parses(
    parsers: Sequence[Tuple[
        Text,
        Callable[[ParseOptions, Context, Text], Arg]
    ]],
    options: ParseOptions,
    context: Context,
    t: Text,
) -> Arg:
  """Try several parsers (in order) on the same piece of text.

  Attempts to run each parser on `t`, passing in the `options`, `context`, and
  `t` arguments directly. If any parser succeeds, all results and errors from
  the preceding, failing parsers are discarded.

  Args:
    parsers: An iterable of (text, parser function) pairs. The text is only used
        to create an error message in the event that all parsers fail. Note that
        `parse_integer` and `parse_string` cannot be specified here, since they
        returns an int and a bytes object respectively.
    options: argument parsing options.
    context: current assembler context.
    t: text data to parse as an integer.

  Returns:
    The Arg result of the first successful parser to return an Arg whose argtype
    is not UNPARSED, or the result of the last successful parser (which in this
    case must be returning an UNPARSED Arg).

  Raises:
    ValueError: none of the parsers have successfully parsed `t`.
  """
  errors: List[Tuple[Text, Exception]] = []

  for description, parser in parsers:
    try:
      return parser(options, context, t)
    except ValueError as e:
      errors.append((description, e))

  raise ValueError('Failed to parse {!r}:\n'.format(t) + '\n'.join(
      '  {}: {}'.format(description, e) for description, e in errors))
