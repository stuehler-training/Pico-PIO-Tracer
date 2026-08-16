from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .encoding import EncodingError, decode_instruction, encode_instruction
from .model import (
    IN_HIGH,
    IN_LOW,
    JOIN_NONE,
    JOIN_RX,
    JOIN_TX,
    OUT_HIGH,
    OUT_LOW,
    SHIFT_LEFT,
    SHIFT_RIGHT,
    Instruction,
    PIOProgram,
    StateMachineConfig,
    normalise_init,
    normalise_threshold,
    u32,
)

PIO_SYMBOLS = {
    "gpio",
    "pin",
    "irq",
    "pins",
    "x",
    "y",
    "null",
    "pindirs",
    "pc",
    "status",
    "isr",
    "osr",
    "exec",
    "not_x",
    "x_dec",
    "not_y",
    "y_dec",
    "x_not_y",
    "not_osre",
    "noblock",
    "block",
    "iffull",
    "ifempty",
    "clear",
}

PIO_CONSTANTS = {
    "IN_LOW": IN_LOW,
    "IN_HIGH": IN_HIGH,
    "OUT_LOW": OUT_LOW,
    "OUT_HIGH": OUT_HIGH,
    "SHIFT_LEFT": SHIFT_LEFT,
    "SHIFT_RIGHT": SHIFT_RIGHT,
    "JOIN_NONE": JOIN_NONE,
    "JOIN_TX": JOIN_TX,
    "JOIN_RX": JOIN_RX,
}

SUPPORTED_INSTRUCTIONS = {"word", "nop", "jmp", "wait", "in_", "out", "push", "pull", "mov", "irq", "set"}
SUPPORTED_DIRECTIVES = {"wrap_target", "wrap", "label"}


class PIOParseError(ValueError):
    pass


@dataclass(slots=True)
class ParsedSource:
    source_path: str | None
    programs: dict[str, PIOProgram]
    machines: list[StateMachineConfig]
    warnings: list[str] = field(default_factory=list)

    def choose(self, *, program_name: str | None = None, sm_id: int | None = None) -> StateMachineConfig:
        candidates = self.machines
        if sm_id is not None:
            candidates = [machine for machine in candidates if machine.sm_id == sm_id]
            if not candidates:
                raise PIOParseError(f"no parsed StateMachine has id {sm_id}")
        if program_name is not None:
            candidates = [machine for machine in candidates if machine.program.name == program_name]
            if candidates:
                return candidates[0]
            if program_name not in self.programs:
                raise PIOParseError(f"no @asm_pio program named {program_name!r}")
            return _synthetic_machine(self.programs[program_name], self.warnings)
        if candidates:
            return candidates[0]
        if not self.programs:
            raise PIOParseError("the file contains no @rp2.asm_pio program")
        return _synthetic_machine(next(iter(self.programs.values())), self.warnings)


@dataclass(slots=True)
class _MachineDraft:
    variable: str
    sm_id: int
    program_name: str | None
    kwargs: dict[str, Any]
    active_seen: bool = False
    initial_tx: list[int] = field(default_factory=list)
    initial_exec: list[int | str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _SafeEvaluator:
    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(values or {})

    def child(self) -> "_SafeEvaluator":
        return _SafeEvaluator(self.values)

    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, f"_eval_{type(node).__name__}", None)
        if method is None:
            raise PIOParseError(f"unsupported static expression {type(node).__name__} at line {getattr(node, 'lineno', '?')}")
        return method(node)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float, bool, str, type(None))):
            return node.value
        raise PIOParseError(f"unsupported literal at line {node.lineno}")

    def _eval_Name(self, node: ast.Name) -> Any:
        if node.id in self.values:
            return self.values[node.id]
        if node.id in PIO_SYMBOLS:
            return node.id
        if node.id in {"True", "False", "None"}:
            return {"True": True, "False": False, "None": None}[node.id]
        raise PIOParseError(f"unknown static name {node.id!r} at line {node.lineno}")

    def _eval_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr in PIO_CONSTANTS:
            return PIO_CONSTANTS[node.attr]
        # machine.Pin.board.GP0 and Pin.cpu.GPIO0 are intentionally conservative.
        if node.attr.startswith("GP") and node.attr[2:].isdigit():
            return int(node.attr[2:])
        if node.attr.startswith("GPIO") and node.attr[4:].isdigit():
            return int(node.attr[4:])
        raise PIOParseError(f"unsupported attribute expression at line {node.lineno}: {_unparse(node)}")

    def _eval_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.eval(item) for item in node.elts)

    def _eval_List(self, node: ast.List) -> list[Any]:
        return [self.eval(item) for item in node.elts]

    def _eval_Set(self, node: ast.Set) -> set[Any]:
        return {self.eval(item) for item in node.elts}

    def _eval_Dict(self, node: ast.Dict) -> dict[Any, Any]:
        return {self.eval(key): self.eval(value) for key, value in zip(node.keys, node.values) if key is not None}

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.eval(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.Invert):
            return ~value
        if isinstance(node.op, ast.Not):
            return not value
        raise PIOParseError(f"unsupported unary operator at line {node.lineno}")

    def _eval_BinOp(self, node: ast.BinOp) -> Any:
        left = self.eval(node.left)
        right = self.eval(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.Pow):
            return left**right
        if isinstance(op, ast.LShift):
            return left << right
        if isinstance(op, ast.RShift):
            return left >> right
        if isinstance(op, ast.BitOr):
            return left | right
        if isinstance(op, ast.BitAnd):
            return left & right
        if isinstance(op, ast.BitXor):
            return left ^ right
        raise PIOParseError(f"unsupported binary operator at line {node.lineno}")

    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.eval(value) for value in node.values]
        if isinstance(node.op, ast.And):
            result: Any = values[0]
            for value in values[1:]:
                result = result and value
            return result
        result = values[0]
        for value in values[1:]:
            result = result or value
        return result

    def _eval_Compare(self, node: ast.Compare) -> bool:
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.eval(comparator)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            else:
                raise PIOParseError(f"unsupported comparison at line {node.lineno}")
            if not ok:
                return False
            left = right
        return True

    def _eval_IfExp(self, node: ast.IfExp) -> Any:
        return self.eval(node.body if self.eval(node.test) else node.orelse)

    def _eval_Subscript(self, node: ast.Subscript) -> Any:
        value = self.eval(node.value)
        index = self.eval(node.slice)
        return value[index]

    def _eval_Call(self, node: ast.Call) -> Any:
        name = _terminal_name(node.func)

        # A Pin object is used by StateMachine only as a GPIO identifier.  Do
        # not evaluate its mode/pull/value arguments: expressions such as
        # Pin(17, Pin.OUT, Pin.PULL_DOWN) are valid MicroPython, but those pad
        # configuration constants are irrelevant to PIO pin-base resolution.
        # Evaluating them first would incorrectly reject the complete Pin call.
        if name == "Pin":
            id_nodes = list(node.args[:1])
            id_nodes.extend(kw.value for kw in node.keywords if kw.arg == "id")
            if len(id_nodes) != 1:
                raise PIOParseError(
                    f"Pin() requires one statically resolvable id at line {node.lineno}: {_unparse(node)}"
                )
            try:
                return int(self.eval(id_nodes[0]))
            except (PIOParseError, TypeError, ValueError) as exc:
                raise PIOParseError(
                    f"Pin() id is not a statically resolvable integer at line {node.lineno}: {_unparse(node)}"
                ) from exc

        args = [self.eval(arg) for arg in node.args]
        kwargs = {kw.arg: self.eval(kw.value) for kw in node.keywords if kw.arg is not None}
        if name in {"const", "int", "float", "bool"} and len(args) == 1 and not kwargs:
            return {"const": lambda x: x, "int": int, "float": float, "bool": bool}[name](args[0])
        if name in {"invert", "reverse", "rel"} and len(args) == 1 and not kwargs:
            return (name, args[0])
        if name == "range":
            return range(*[int(arg) for arg in args])
        if name in {"tuple", "list"} and len(args) == 1:
            return tuple(args[0]) if name == "tuple" else list(args[0])
        if name in {"array", "bytearray"}:
            if len(args) >= 2:
                return list(args[1])
            if args:
                return list(args[0])
        raise PIOParseError(f"unsupported function call in static expression at line {node.lineno}: {_unparse(node)}")


class _ProgramBuilder:
    def __init__(
        self,
        name: str,
        source: str,
        source_path: str | None,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        decorator_values: dict[str, Any],
        globals_: Mapping[str, Any],
    ) -> None:
        self.name = name
        self.source = source
        self.source_path = source_path
        self.function = function
        self.decorator_values = decorator_values
        self.evaluator = _SafeEvaluator(globals_)
        self.instructions: list[Instruction] = []
        self.labels: dict[str, int] = {}
        self.wrap_target = 0
        self.wrap_top: int | None = None
        self.wrap_seen = False
        self.warnings: list[str] = []

    def build(self) -> PIOProgram:
        self._statements(self.function.body, self.evaluator)
        if self.wrap_top is None and self.instructions:
            self.wrap_top = len(self.instructions) - 1
        defaults = {
            "out_init": None,
            "set_init": None,
            "sideset_init": None,
            "side_pindir": False,
            "in_shiftdir": SHIFT_LEFT,
            "out_shiftdir": SHIFT_LEFT,
            "autopush": False,
            "autopull": False,
            "push_thresh": 32,
            "pull_thresh": 32,
            "fifo_join": JOIN_NONE,
        }
        unknown = set(self.decorator_values) - set(defaults)
        if unknown:
            raise PIOParseError(f"unsupported asm_pio decorator argument(s) for {self.name}: {', '.join(sorted(unknown))}")
        defaults.update(self.decorator_values)
        sideset_init = normalise_init(defaults["sideset_init"])
        sideset_count = len(sideset_init or ())
        sideset_optional = bool(sideset_count and any(instruction.side is None for instruction in self.instructions))

        program = PIOProgram(
            name=self.name,
            instructions=self.instructions,
            labels=self.labels,
            wrap_target=self.wrap_target,
            wrap_top=self.wrap_top,
            out_init=normalise_init(defaults["out_init"]),
            set_init=normalise_init(defaults["set_init"]),
            sideset_init=sideset_init,
            side_pindir=bool(defaults["side_pindir"]),
            in_shiftdir=int(defaults["in_shiftdir"]),
            out_shiftdir=int(defaults["out_shiftdir"]),
            autopush=bool(defaults["autopush"]),
            autopull=bool(defaults["autopull"]),
            push_thresh=normalise_threshold(int(defaults["push_thresh"])),
            pull_thresh=normalise_threshold(int(defaults["pull_thresh"])),
            fifo_join=int(defaults["fifo_join"]),
            sideset_optional=sideset_optional,
            source_path=self.source_path,
            source_line=self.function.lineno,
            source_end_line=getattr(self.function, "end_lineno", None),
            source_text=self.source,
            warnings=self.warnings,
        )
        for instruction in program.instructions:
            if instruction.op == "word":
                instruction.word = encode_instruction(instruction, program)
                decoded = decode_instruction(instruction.word, program)
                decoded.pc = instruction.pc
                decoded.source_line = instruction.source_line
                decoded.source_text = instruction.source_text or f"word(0x{instruction.word:04x})"
                program.instructions[instruction.pc or 0] = decoded
            else:
                instruction.word = encode_instruction(instruction, program)
        return program

    def _statements(self, statements: Iterable[ast.stmt], evaluator: _SafeEvaluator) -> None:
        for statement in statements:
            if isinstance(statement, ast.Expr):
                if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                    continue
                self._expression(statement.value, evaluator)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target, value_node = _assignment_parts(statement)
                if not isinstance(target, ast.Name) or value_node is None:
                    raise PIOParseError(f"only simple local assignments are supported inside PIO programs (line {statement.lineno})")
                evaluator.values[target.id] = evaluator.eval(value_node)
            elif isinstance(statement, ast.AugAssign):
                if not isinstance(statement.target, ast.Name):
                    raise PIOParseError(f"only simple augmented assignments are supported at line {statement.lineno}")
                current = evaluator.values.get(statement.target.id)
                if current is None:
                    raise PIOParseError(f"unknown augmented-assignment name {statement.target.id!r} at line {statement.lineno}")
                synthetic = ast.BinOp(ast.Constant(current), statement.op, statement.value)
                evaluator.values[statement.target.id] = evaluator.eval(synthetic)
            elif isinstance(statement, ast.For):
                if not isinstance(statement.target, ast.Name):
                    raise PIOParseError(f"only a simple loop variable is supported at line {statement.lineno}")
                iterable = evaluator.eval(statement.iter)
                values = list(iterable)
                if len(values) > 4096:
                    raise PIOParseError(f"static PIO loop at line {statement.lineno} expands to too many iterations")
                for value in values:
                    child = evaluator.child()
                    child.values[statement.target.id] = value
                    self._statements(statement.body, child)
                if statement.orelse:
                    self._statements(statement.orelse, evaluator.child())
            elif isinstance(statement, ast.If):
                branch = statement.body if evaluator.eval(statement.test) else statement.orelse
                self._statements(branch, evaluator.child())
            elif isinstance(statement, (ast.Pass, ast.Return)):
                if isinstance(statement, ast.Return) and statement.value is not None:
                    raise PIOParseError(f"PIO program return values are unsupported at line {statement.lineno}")
            else:
                raise PIOParseError(
                    f"unsupported Python statement {type(statement).__name__} inside PIO program {self.name!r} at line {statement.lineno}"
                )

    def _expression(self, expression: ast.AST, evaluator: _SafeEvaluator) -> None:
        base, delay, side = _peel_modifiers(expression, evaluator)
        if not isinstance(base, ast.Call):
            raise PIOParseError(f"expected a PIO instruction/directive at line {getattr(expression, 'lineno', '?')}")
        name = _terminal_name(base.func)
        args = [evaluator.eval(arg) for arg in base.args]
        kwargs = {kw.arg: evaluator.eval(kw.value) for kw in base.keywords if kw.arg is not None}
        if kwargs:
            raise PIOParseError(f"PIO instruction keyword arguments are unsupported at line {base.lineno}")

        if name in SUPPORTED_DIRECTIVES:
            if delay is not None or side is not None:
                raise PIOParseError(f"assembler directive {name} cannot have delay/side-set modifiers")
            self._directive(name, args, base)
            return
        if name not in SUPPORTED_INSTRUCTIONS:
            raise PIOParseError(f"unsupported PIO instruction {name!r} at line {base.lineno}")

        semantic_name = "in" if name == "in_" else name
        semantic_args = self._normalise_instruction_args(semantic_name, args, base)
        instruction = Instruction(
            semantic_name,
            semantic_args,
            delay=0 if delay is None else int(delay),
            side=None if side is None else int(side),
            source_line=base.lineno,
            source_text=_unparse(expression),
        )
        self.instructions.append(instruction)

    def _directive(self, name: str, args: list[Any], node: ast.Call) -> None:
        if name == "label":
            if len(args) != 1 or not isinstance(args[0], str):
                raise PIOParseError(f"label() requires one string at line {node.lineno}")
            label = args[0]
            if label in self.labels:
                raise PIOParseError(f"duplicate PIO label {label!r} at line {node.lineno}")
            self.labels[label] = len(self.instructions)
        elif name == "wrap_target":
            if args:
                raise PIOParseError(f"wrap_target() takes no arguments at line {node.lineno}")
            self.wrap_target = len(self.instructions)
        elif name == "wrap":
            if args:
                raise PIOParseError(f"wrap() takes no arguments at line {node.lineno}")
            if not self.instructions:
                raise PIOParseError(f"wrap() cannot precede every instruction at line {node.lineno}")
            self.wrap_top = len(self.instructions) - 1
            self.wrap_seen = True

    def _normalise_instruction_args(self, name: str, args: list[Any], node: ast.Call) -> tuple[Any, ...]:
        line = node.lineno
        if name == "nop":
            _require_count(name, args, 0, line)
            return ()
        if name == "jmp":
            if len(args) == 1:
                return ("always", args[0])
            _require_count(name, args, 2, line)
            return (str(args[0]), args[1])
        if name == "wait":
            _require_count(name, args, 3, line)
            source = args[1]
            if source == "pin":
                source_name = "pin"
            elif source == "gpio" or source == 0:
                source_name = "gpio"
            else:
                source_name = "irq"
            index = args[2]
            relative = False
            if isinstance(index, tuple) and index and index[0] == "rel":
                relative = True
                index = index[1]
            if relative and source_name != "irq":
                raise PIOParseError(f"rel() is only valid for WAIT IRQ at line {line}")
            return (int(args[0]), source_name, int(index), relative)
        if name == "in":
            _require_count(name, args, 2, line)
            return (str(args[0]), int(args[1]))
        if name == "out":
            _require_count(name, args, 2, line)
            return (str(args[0]), int(args[1]))
        if name in {"push", "pull"}:
            if len(args) > 2:
                raise PIOParseError(f"{name}() accepts at most two modifiers at line {line}")
            conditional_name = "iffull" if name == "push" else "ifempty"
            names = [str(arg) for arg in args]
            allowed = {conditional_name, "block", "noblock"}
            unknown = [modifier for modifier in names if modifier not in allowed]
            if unknown:
                raise PIOParseError(f"invalid {name} modifier {unknown[0]!r} at line {line}")
            if "block" in names and "noblock" in names:
                raise PIOParseError(f"{name}() cannot specify both block and noblock at line {line}")
            if len(names) != len(set(names)):
                raise PIOParseError(f"duplicate {name} modifier at line {line}")
            conditional = conditional_name in names
            block = "noblock" not in names
            return (conditional, block)
        if name == "mov":
            _require_count(name, args, 2, line)
            source: Any = args[1]
            if isinstance(source, tuple):
                operation, operand = source
                if operation not in {"invert", "reverse"}:
                    raise PIOParseError(f"invalid mov operation {operation!r} at line {line}")
                source = (operation, str(operand))
            else:
                source = str(source)
            return (str(args[0]), source)
        if name == "irq":
            if len(args) == 1:
                action = "set"
                index = args[0]
            elif len(args) == 2:
                modifier = str(args[0])
                if modifier not in {"clear", "block", "noblock"}:
                    raise PIOParseError(f"invalid irq modifier {modifier!r} at line {line}")
                action = "clear" if modifier == "clear" else "wait" if modifier == "block" else "set"
                index = args[1]
            else:
                raise PIOParseError(f"irq() takes one or two arguments at line {line}")
            relative = False
            if isinstance(index, tuple) and index and index[0] == "rel":
                relative = True
                index = index[1]
            return (action, int(index), relative)
        if name == "set":
            _require_count(name, args, 2, line)
            return (str(args[0]), int(args[1]))
        if name == "word":
            if not 1 <= len(args) <= 2:
                raise PIOParseError(f"word() takes a value and optional label at line {line}")
            return tuple(args)
        raise AssertionError(name)


def parse_file(path: str | Path) -> ParsedSource:
    source_path = str(Path(path))
    source = Path(path).read_text(encoding="utf-8")
    return parse_source(source, source_path=source_path)


def parse_source(source: str, *, source_path: str | None = None) -> ParsedSource:
    try:
        module = ast.parse(source, filename=source_path or "<pio-source>")
    except SyntaxError as exc:
        raise PIOParseError(str(exc)) from exc

    globals_, global_warnings = _collect_globals(module)
    programs: dict[str, PIOProgram] = {}
    for statement in module.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator = _find_asm_pio_decorator(statement)
        if decorator is None:
            continue
        if isinstance(statement, ast.AsyncFunctionDef):
            raise PIOParseError(f"PIO program {statement.name!r} cannot be async")
        evaluator = _SafeEvaluator(globals_)
        decorator_values = {
            keyword.arg: evaluator.eval(keyword.value)
            for keyword in decorator.keywords
            if keyword.arg is not None
        }
        if decorator.args:
            raise PIOParseError(f"asm_pio only accepts keyword arguments (line {decorator.lineno})")
        builder = _ProgramBuilder(statement.name, source, source_path, statement, decorator_values, globals_)
        programs[statement.name] = builder.build()

    drafts, machine_warnings = _collect_machine_drafts(module, globals_, programs)
    machines: list[StateMachineConfig] = []
    for draft in drafts:
        program = programs.get(draft.program_name) if draft.program_name is not None else None
        if program is None:
            if draft.program_name is None:
                machine_warnings.append(
                    f"StateMachine variable {draft.variable!r} has no statically identifiable program; call .init(program, ...) or pass one to the constructor"
                )
            else:
                machine_warnings.append(
                    f"StateMachine variable {draft.variable!r} references {draft.program_name!r}, which is not a parsed @asm_pio program"
                )
            continue
        machines.append(_resolve_machine(draft, program))

    return ParsedSource(source_path, programs, machines, [*global_warnings, *machine_warnings])


def parse_instruction_text(text: str, program: PIOProgram) -> Instruction:
    """Parse one MicroPython PIO instruction string used by StateMachine.exec()."""
    expression = ast.parse(text, mode="eval").body
    dummy_function = ast.FunctionDef(
        name="_exec",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[],
        decorator_list=[],
    )
    builder = _ProgramBuilder("_exec", text, None, dummy_function, {}, {})
    builder._expression(expression, builder.evaluator)
    if len(builder.instructions) != 1:
        raise PIOParseError("exec string must contain one PIO instruction")
    instruction = builder.instructions[0]
    # MicroPython asm_pio_encode(), which backs StateMachine.exec(str), does not
    # perform the whole-program mandatory-side-set consistency check.
    instruction.word = encode_instruction(instruction, program, allow_missing_sideset=True)
    return instruction


def _collect_globals(module: ast.Module) -> tuple[dict[str, Any], list[str]]:
    evaluator = _SafeEvaluator()
    warnings: list[str] = []
    changed = True
    # A few passes allow constants to refer to constants declared earlier/later without executing code.
    for _ in range(4):
        if not changed:
            break
        changed = False
        for statement in module.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            target, value_node = _assignment_parts(statement)
            if not isinstance(target, ast.Name) or value_node is None:
                continue
            if _looks_like_state_machine_call(value_node):
                continue
            try:
                value = evaluator.eval(value_node)
            except PIOParseError:
                continue
            if evaluator.values.get(target.id, object()) != value:
                evaluator.values[target.id] = value
                changed = True
    return evaluator.values, warnings


def _collect_machine_drafts(
    module: ast.Module,
    globals_: Mapping[str, Any],
    programs: Mapping[str, PIOProgram],
) -> tuple[list[_MachineDraft], list[str]]:
    evaluator = _SafeEvaluator(globals_)
    drafts: dict[str, _MachineDraft] = {}
    warnings: list[str] = []

    def process(statements: Iterable[ast.stmt], local_eval: _SafeEvaluator) -> None:
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target, value_node = _assignment_parts(statement)
                if isinstance(target, ast.Name) and isinstance(value_node, ast.Call) and _looks_like_state_machine_call(value_node):
                    try:
                        draft = _parse_machine_constructor(target.id, value_node, local_eval, programs)
                    except PIOParseError as exc:
                        warnings.append(str(exc))
                    else:
                        drafts[target.id] = draft
                    continue
                if isinstance(target, ast.Name) and value_node is not None:
                    try:
                        local_eval.values[target.id] = local_eval.eval(value_node)
                    except PIOParseError:
                        pass
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                _process_machine_method(statement.value, drafts, local_eval, programs, warnings)
            elif isinstance(statement, ast.For):
                if not isinstance(statement.target, ast.Name):
                    continue
                try:
                    values = list(local_eval.eval(statement.iter))
                except PIOParseError:
                    continue
                if len(values) > 4096:
                    warnings.append(f"ignored very large top-level loop at line {statement.lineno}")
                    continue
                for value in values:
                    child = local_eval.child()
                    child.values[statement.target.id] = value
                    process(statement.body, child)
            elif isinstance(statement, ast.If):
                try:
                    condition = local_eval.eval(statement.test)
                except PIOParseError:
                    # For a conventional __name__ guard, analyse the body as the executed script path.
                    if _is_main_guard(statement.test):
                        process(statement.body, local_eval.child())
                    else:
                        _warn_ignored_runtime_machine_calls(statement, drafts, warnings)
                    continue
                process(statement.body if condition else statement.orelse, local_eval.child())
            elif isinstance(statement, ast.While):
                try:
                    if not bool(local_eval.eval(statement.test)):
                        continue
                except PIOParseError:
                    pass
                _warn_ignored_runtime_machine_calls(statement, drafts, warnings)

    process(module.body, evaluator)
    return list(drafts.values()), warnings


def _warn_ignored_runtime_machine_calls(
    statement: ast.stmt,
    drafts: Mapping[str, _MachineDraft],
    warnings: list[str],
) -> None:
    calls: set[str] = set()
    for node in ast.walk(statement):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id in drafts:
            calls.add(f"{owner.id}.{node.func.attr}()")
    if calls:
        warnings.append(
            f"runtime Python control flow at line {statement.lineno} is not executed by the static parser; "
            f"ignored StateMachine call(s): {', '.join(sorted(calls))}. "
            "Use a stimulus JSON file to schedule GPIO and FIFO activity"
        )



def _parse_machine_constructor(
    variable: str,
    call: ast.Call,
    evaluator: _SafeEvaluator,
    programs: Mapping[str, PIOProgram],
) -> _MachineDraft:
    name = _terminal_name(call.func)
    if name not in {"StateMachine", "state_machine"}:
        raise PIOParseError(f"unsupported state-machine constructor at line {call.lineno}")
    if not call.args:
        raise PIOParseError(f"StateMachine needs an id at line {call.lineno}")
    if len(call.args) > 3:
        raise PIOParseError(
            f"StateMachine accepts id, program, and optional freq positionally at line {call.lineno}"
        )
    sm_id = int(evaluator.eval(call.args[0]))
    program_name = _program_reference_name(call.args[1], programs) if len(call.args) >= 2 else None
    kwargs: dict[str, Any] = {}
    if len(call.args) >= 3:
        kwargs["freq"] = evaluator.eval(call.args[2])
    for keyword in call.keywords:
        if keyword.arg is None:
            raise PIOParseError(f"**kwargs is unsupported in StateMachine at line {call.lineno}")
        if keyword.arg in kwargs:
            raise PIOParseError(f"StateMachine got multiple values for {keyword.arg!r} at line {call.lineno}")
        kwargs[keyword.arg] = evaluator.eval(keyword.value)
    return _MachineDraft(variable, sm_id, program_name, kwargs)


def _process_machine_method(
    call: ast.Call,
    drafts: dict[str, _MachineDraft],
    evaluator: _SafeEvaluator,
    programs: Mapping[str, PIOProgram],
    warnings: list[str],
) -> None:
    if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name):
        return
    variable = call.func.value.id
    draft = drafts.get(variable)
    if draft is None:
        return
    method = call.func.attr
    if method == "init":
        if not call.args:
            warnings.append(f"ignored {variable}.init() without a program at line {call.lineno}")
            return
        if len(call.args) > 2:
            warnings.append(f"ignored {variable}.init() with too many positional arguments at line {call.lineno}")
            return
        try:
            program_name = _program_reference_name(call.args[0], programs)
            kwargs: dict[str, Any] = {}
            if len(call.args) == 2:
                kwargs["freq"] = evaluator.eval(call.args[1])
            for keyword in call.keywords:
                if keyword.arg is None:
                    raise PIOParseError("**kwargs is unsupported")
                if keyword.arg in kwargs:
                    raise PIOParseError(f"multiple values for {keyword.arg!r}")
                kwargs[keyword.arg] = evaluator.eval(keyword.value)
        except PIOParseError as exc:
            warnings.append(f"ignored dynamic {variable}.init call at line {call.lineno}: {exc}")
            return
        draft.program_name = program_name
        draft.kwargs.update(kwargs)
        return
    try:
        args = [evaluator.eval(arg) for arg in call.args]
        kwargs = {kw.arg: evaluator.eval(kw.value) for kw in call.keywords if kw.arg is not None}
    except PIOParseError as exc:
        warnings.append(f"ignored dynamic {variable}.{method} call at line {call.lineno}: {exc}")
        return
    if method == "active" and args:
        if bool(args[0]):
            draft.active_seen = True
    elif method == "put" and args:
        values = args[0]
        shift = int(kwargs.get("shift", args[1] if len(args) > 1 else 0))
        if isinstance(values, (list, tuple)):
            sequence = values
        else:
            sequence = [values]
        for value in sequence:
            draft.initial_tx.append(u32(int(value) << shift))
        if draft.active_seen:
            draft.warnings.append(
                f"{variable}.put() at line {call.lineno} is treated as a cycle-zero host write; use a stimulus JSON file for exact timing"
            )
    elif method == "exec" and args:
        draft.initial_exec.append(args[0])
        if draft.active_seen:
            draft.warnings.append(
                f"{variable}.exec() at line {call.lineno} is treated as initial setup; runtime forced-instruction timing is not inferred from Python execution"
            )
    elif method in {"get", "rx_fifo", "tx_fifo", "irq", "restart"}:
        draft.warnings.append(f"top-level {variable}.{method}() at line {call.lineno} is not scheduled automatically")


def _resolve_machine(draft: _MachineDraft, program: PIOProgram) -> StateMachineConfig:
    kwargs = dict(draft.kwargs)
    known = {
        "freq",
        "in_base",
        "out_base",
        "set_base",
        "jmp_pin",
        "sideset_base",
        "in_shiftdir",
        "out_shiftdir",
        "push_thresh",
        "pull_thresh",
    }
    unknown = sorted(set(kwargs) - known)
    warnings = list(draft.warnings)
    if unknown:
        warnings.append(f"ignored unsupported StateMachine keyword(s): {', '.join(unknown)}")
    if "freq" in kwargs:
        frequency = float(kwargs["freq"])
        if frequency < 0:
            frequency = 125_000_000.0
            warnings.append(
                "StateMachine freq=-1 selects the system clock; trace timing assumes the RP2040 default of 125 MHz"
            )
    else:
        frequency = 125_000_000.0
        warnings.append("StateMachine frequency was omitted; trace timing assumes a 125 MHz RP2040 system clock")

    initial_exec_words: list[int] = []
    for value in draft.initial_exec:
        try:
            if isinstance(value, str):
                initial_exec_words.append(parse_instruction_text(value, program).word or 0)
            else:
                initial_exec_words.append(int(value) & 0xFFFF)
        except (PIOParseError, EncodingError, ValueError) as exc:
            warnings.append(f"could not parse initial StateMachine.exec({value!r}): {exc}")

    config = StateMachineConfig(
        program=program,
        sm_id=draft.sm_id,
        requested_freq_hz=frequency,
        in_base=int(kwargs.get("in_base", 0)),
        out_base=_optional_int(kwargs.get("out_base")),
        set_base=_optional_int(kwargs.get("set_base")),
        sideset_base=_optional_int(kwargs.get("sideset_base")),
        jmp_pin=int(kwargs.get("jmp_pin", 0)),
        initial_tx=draft.initial_tx,
        initial_exec=initial_exec_words,
        warnings=warnings,
        in_shiftdir_override=_optional_int(kwargs.get("in_shiftdir")),
        out_shiftdir_override=_optional_int(kwargs.get("out_shiftdir")),
        push_thresh_override=_optional_int(kwargs.get("push_thresh")),
        pull_thresh_override=_optional_int(kwargs.get("pull_thresh")),
    )
    if program.out_init is not None and config.out_base is None:
        config.warnings.append("out_init is present but no out_base was parsed; hardware OUT/MOV PINS writes have OUT_COUNT=0")
    if program.set_init is not None and config.set_base is None:
        config.warnings.append("set_init is present but no set_base was parsed; hardware SET PINS/PINDIRS writes have SET_COUNT=0")
    if program.sideset_init is not None and config.sideset_base is None:
        config.warnings.append("sideset_init is present but no sideset_base was parsed; side-set pin mapping is inactive")
    if len(config.initial_tx) > config.tx_capacity:
        config.warnings.append(
            f"{len(config.initial_tx)} initial TX words exceed FIFO capacity {config.tx_capacity}; "
            "the emulator retains excess words as blocked host writes. In real Python, blocking put() "
            "calls made before the state machine is active can prevent later code from running"
        )
    return config


def _synthetic_machine(program: PIOProgram, inherited_warnings: Iterable[str]) -> StateMachineConfig:
    warnings = list(inherited_warnings)
    warnings.append(
        "no StateMachine construction was parsed; using a synthetic 1 MHz configuration with configured pin groups based at GPIO0"
    )
    return StateMachineConfig(
        program=program,
        sm_id=0,
        requested_freq_hz=1_000_000,
        in_base=0,
        out_base=0 if program.out_init is not None else None,
        set_base=0 if program.set_init is not None else None,
        sideset_base=0 if program.sideset_init is not None else None,
        jmp_pin=0,
        warnings=warnings,
    )


def _find_asm_pio_decorator(function: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Call | None:
    for decorator in function.decorator_list:
        if isinstance(decorator, ast.Call) and _terminal_name(decorator.func) == "asm_pio":
            return decorator
    return None


def _peel_modifiers(expression: ast.AST, evaluator: _SafeEvaluator) -> tuple[ast.AST, int | None, int | None]:
    node = expression
    delay: int | None = None
    side: int | None = None
    while True:
        if isinstance(node, ast.Subscript):
            if delay is not None:
                raise PIOParseError(f"instruction has more than one delay modifier at line {node.lineno}")
            delay = int(evaluator.eval(node.slice))
            node = node.value
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"side", "delay"}:
            modifier = node.func.attr
            if len(node.args) != 1 or node.keywords:
                raise PIOParseError(f".{modifier}() requires one positional value at line {node.lineno}")
            if modifier == "side":
                if side is not None:
                    raise PIOParseError(f"instruction has more than one side-set modifier at line {node.lineno}")
                side = int(evaluator.eval(node.args[0]))
            else:
                if delay is not None:
                    raise PIOParseError(f"instruction has more than one delay modifier at line {node.lineno}")
                delay = int(evaluator.eval(node.args[0]))
            node = node.func.value
            continue
        break
    return node, delay, side


def _assignment_parts(statement: ast.Assign | ast.AnnAssign) -> tuple[ast.AST | None, ast.AST | None]:
    if isinstance(statement, ast.Assign):
        if len(statement.targets) != 1:
            return None, statement.value
        return statement.targets[0], statement.value
    return statement.target, statement.value


def _terminal_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _program_reference_name(node: ast.AST, programs: Mapping[str, PIOProgram]) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    text = _unparse(node)
    if text in programs:
        return text
    raise PIOParseError(f"cannot identify StateMachine program at line {getattr(node, 'lineno', '?')}: {text}")


def _looks_like_state_machine_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _terminal_name(node.func) in {"StateMachine", "state_machine"}


def _is_main_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "__name__"
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == "__main__"
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _require_count(name: str, args: list[Any], count: int, line: int) -> None:
    if len(args) != count:
        raise PIOParseError(f"{name}() requires {count} argument(s) at line {line}, got {len(args)}")


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return f"<{type(node).__name__}>"
