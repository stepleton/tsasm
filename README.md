# `tsasm`, a small assembler written in pure Python.

If you need a cheap and cheerful assembler for a tiny project on an architecture
that doesn't get much support from "real" assemblers, you might be able to hack
your opcodes into `tsasm` and get on with life. That's what I wrote it for.

## Anti-features

* No macro facilties right now.
* All labels must be unique everywhere.
* Only one source code file; no includes. Write shorter code.
* No `.equ`, `.asg` or `.set` directives or similar. Use numbers.

## Usage

    ./main.py --listing=prog.lst --arch=ibm5100 prog.asm a.out

For now, `tsasm` just dumps bytes to an output file. There are no notions of
sections, headers, framing, metadata, symbol tables, linking, executable
formats, or anything else like that. Your assembly language statements are
turned into bytes, in the locations that your code says that they belong. As
such, if the first line of your program is something like

    ORG  $8000

then your output is going to have 32KiB of $00 bytes before you start to see any
bytes for the code you wrote.

If you're not going to un-dump the generated output straight into RAM and jump
right into it, why are you coding in assembly?

## System requirements

Python 3.6 or greater.

## Language details

`tsasm` was written by someone who knows how to write a lexer and a parser the
right way but couldn't be bothered to reach for the dragon book this time. You
get regexp-based hacks instead, so the code won't be suitable for an undergrad
comp sci course, regrettably. Here, have a "grammar":

* **COMMENTS** start with a bare `;` and go to the end of the line. They are
  preceeded by CODE STUFF.
* **CODE STUFF** can start with a LABEL, or not, and carry on with zero or more
  CODEY BITS.
* **LABELS** start with `_` or a letter and continue with `_` or alphanumeric
  characters. They end with `:`.
* **CODEY BITS** are separated by bare whitespace or bare commas. They can be
  composed of characters that aren't any of those, or of STRINGS.
* **STRINGS** are `"` or `'`-delimited strings---you know. `\` is your escape
  character.

Additionally, data structures inside the assembler call the first CODEY BIT an
"opcode" and the remaining ones "args".

That's about all the structure that `tsasm` imposes out of the box. From there,
architecture-specific code generation modules impose additional structure as
they see fit. `tsasm` does provide these modules with some built-in ways to
interpret certain CODEY BITS, including:

* **ADDRESS** literals, with popular formats like `1234` and `1234d` (decimal
  numbers), `$12AB` and `12ABh` (hexadecimal), `1234o` and `1234q` (octal),
  `1010b` (binary), `"!"` and `'!'` (single characters), plus whatever else
  Python 3 will accept as the first argument `s` in `int(s, base=0)`.
* **NUMBERS**, which are just integers for now, and which are the same format as
  ADDRESS literals but preceded by the `#` character.
* **REGISTERS**, which are the same format as ADDRESS literals but preceded by
  some architecture-specific case-insensitive prefix, like `R` if the arch has
  an orthogonal register file or `A` or `D` if it doesn't. It's possible for the
  prefix not to be followed by the number, as might happen if you use a cursed
  architecture with registers with strange names like `eax`.
* **DEREFERENCES**, which warmly embrace an ADDRESS or a REGISTER in brackets,
  like `(R2)` or `($E842)`. Additionally, any number of `+` or `-` characters
  can be arranged on the outside of the brackets to denote
  (pre/post)-(in/de)crementation. Some architectures may support some notion of
  'crementation by -0.5 or +0.5, and for these you can use `~` or `'`
  respectively.

When parsing any numeric value in an ADDRESS, a NUMBER, or even a REGISTER for
some reason, `tsasm` will detect if the raw text is a LABEL and substitute in
the label's value.

Anyway, code generation modules don't have to use the parsing facilities that
support these idioms, or may only use them on a per-opcode basis.

### Example:

Here is some example parity-calculating code I cranked out for an [IBM
5100](https://en.wikipedia.org/wiki/IBM_5100) computer with its 16-bit
[PALM](https://en.wikipedia.org/wiki/IBM_PALM_processor) processor.

```assembly
; Compute even parity for each byte in the string "HELLO WORLD!".
; Count in R4 the number of bytes with a parity bit of 1, then halt.
; Meditate on living with an 8-bit ALU that can't multiply.

       ARCH  ibm5100        ; Use the ibm5100.py code generation module.
       ORG   $800           ; This program starts at $800.

       ; Main program.
       LBI    R4, #0        ; Load 0 into LSByte of R4 (parity bit count).
       LWI    R3, #Data     ; Load address of the string into R3.
mloop: MOVB   R5, (R3)+     ; Copy next string character into R5.
       SNS    R5            ; Skip next line unless character is $FF.
       HALT                 ;   All done!
       CALL   Parity,R2     ; Compute even parity of R5 byte.
       CLR    R5, #$FE      ; Isolate just the parity bit.
       ADD    R4, R5        ; Add the bit to the parity bit count.
       BRA    mloop         ; Deal with the next byte.

       ; Compute even parity of the lower byte in R5.
       ; Trashes R5-R7. Return address must be in R2.
       ; The computed parity bit is the least-significant bit of R5.
       ; Based on the algorithm described at
       ; http://graphics.stanford.edu/~seander/bithacks.html#ParityParallel
Parity:
       MOVE   R6, R5        ; Copy byte into R6 and do a logical right...
       SWAP   R6            ; ...shift of four bytes: SWAP is a 4-byte...
       CLR    R6, #$F0      ; ...rotate, CLR clears indicated bits.
       XOR    R5, R6        ; XOR byte with the result.
       CLR    R5, #$F0      ; The lower nybble that obtains counts the...
       LBI    R7, #$69      ; ...number of bits to right-shift a magic...
       LBI    R6, #$96      ; ...value $6996 to get even parity in bit 1.

ploop: SZ     R5            ; We can only shift by 1 bit. Are we done?
       RET    R2            ;   Yes, back to caller!
       MLH    R6, R7        ; Move magic word upper-byte to Hi(R6).
       SHR    R6            ; Shift Lo(R6) 1-right, carrying in a Hi bit.
       SHR    R7            ; Shift the magic word upper byte too.
       DEC    R5, R5        ; Decrement shifts remaining.
       BRA    ploop         ; Back to top of loop.

       ; $00 is ' ' for the 5100, so we use $FF as a string terminator.
Data:  .db    "HELLO WORLD!",$FF
```

## Adding an architecture

The `codegen` subdirectory contains architecture-specific code generation
modules. An assembly language program specifies which architecture to use
with an `ARCH` (or `.ARCH`, or `CPU`, or `.CPU`, or variants of same with
lower-case letters) statement. For example,

    .cpu ibm5100

causes the assembler to load the `codegen/ibm5100.py` module. Any such module
defines two functions: `encode_str` and `get_codegen`. The former has this
signature:

    encode_str(data: Text) -> bytes

and is used to turn data fron string constants into strings of bytes as
appropriate for the architecture. Note that `tsasm` allows the specification of
single-character strings as integer literals in addresses or numbers, which may
have important consequences for your encoding. On the other hand, if your
strings must all be ASCII bytes and your numbers are all 8-bit, no problem.

The `get_codegen` function returns a dict from "casefolded" (see `str.casefold`)
opcodes (i.e. the first CODEY BIT in a statement) to functions that generate hex
data for those opcodes:

    get_codegen() -> Dict[Text, Callable[[Context, Op], Tuple[Context, Op]]]

where `Context` and `Op` are classes defined in the `data.py` module. It might
build out this dict with entries like this one:

    'xor': _codegen_xor,

where `_codegen_xor` is another function in the module that specialises in
generating binary code for the `XOR` opcode. A heavily-annotated implementation
of `_codegen_xor` for the old IBM PALM processor could look like this:

```python
def _codegen_xor(context: Context, op: Op) -> Tuple[Context, Op]:
  # The first thing we'll want to do is parse the arguments in this statement,
  # the details of which are stored in `op`. The PALM ALU can only operate on
  # registers, so both arguments have to be registers. We can use
  # `parse_args_if_able` from the `data` module to do most of the hard work.
  # Note how we're replacing `op` here: both `context` and `op` are namedtuples
  # and cannot be mutated.
  op = op._replace(args=parse_args_if_able(
      _PARSE_OPTIONS, context, op, Type.REGISTER, Type.REGISTER))

  # If all arguments were parsed successfully, we can try generating some code.
  # Reasons for args not being parsed successfully include using labels that
  # haven't been bound to a value yet because they are defined later in the
  # file. In those cases, we'll just have to wait for the next code generation
  # pass to try again.
  if all_args_parsed(op.args):  # This is another `data` module helper.

    # Check to make sure the registers are in bounds. ValueError is the
    # exception to use to object to a user's code.
    if not 0 <= op.args[0].integer <= 15: raise ValueError('Bad 1st register.')
    if not 0 <= op.args[1].integer <= 15: raise ValueError('Bad 2nd register.')

    # With good arguments we are ready to make code. This means supplying
    # hexadecimal strings as the `hex` field of `op`. For the IBM 5100, xor
    # instructions take the form 0ab7, where a and b are registers.
    op = op._replace(hex='0{:X}{:X}7'.format(
        op.args[0].integer, op.args[1].integer))

    # Since we've generated all the code that we need to generate for this
    # statement, we set the `todo` field of `op` to None. If we didn't do this,
    # `_codegen_xor` would be called again for this statement in the next pass.
    # The assembler will continue making passes through the code until all
    # statements have None todos.
    op = op._replace(todo=None)

  # Even if we weren't able to generate code, we know that the code we would
  # generate would occupy two bytes. We advance our current location in the code
  # to reflect this:
  context = context.advance_by_bytes(2)

  return context, op
```
Some additional details for understanding the example:

* `_PARSE_OPTIONS` is a `data.ParseOptions` namedtuple, and for `_codegen_xor`
  it mainly serves to tell `parse_args_if_able` that register names start with
  `R`.
* All `op.args` entries are of type `data.Arg` and include type information
  in the form of `data.Type` values. These values are `enum.Flag`s, allowing the
  type to include `Type.UNPARSED`, a qualifier indicating that parsing the
  argument could not be completed (e.g. due to a label being undefined).
* `data.Type` values can also tell `parse_args_if_able` what type of args might
  be acceptable at a certain position. Multiple single types can be combined
  with `|`, e.g. `Type.REGISTER | Type.DEREF_REGISTER`.

For further information that will help you understand how to write code
generation modules, study the data structures and documentation in `data.py` and
have a look through any of the existing modules.

## Disclaimer

This is not an official Google product.
