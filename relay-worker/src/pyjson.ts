/**
 * JSON as Python's `json` module reads and writes it, for values whose bytes are hashed.
 *
 * The archive's revision is the SHA-256 of `json.dumps(manifest, ensure_ascii=False,
 * sort_keys=True, separators=(",", ":"))` after `json.loads` on the client's body. Two details
 * break a JavaScript round trip: Python keeps `1.0` a float (written `1.0`, JS writes `1`) and
 * an integer exact at any size, and it formats floats with its own `repr`. Numbers therefore
 * keep their source text here, and objects keep Python's key order (first insertion, last value).
 */

export type PyValue = null | boolean | string | PyNumber | PyValue[] | PyObject;

/** A JSON number with its lexeme, so ints and floats re-encode the way Python writes them. */
export class PyNumber {
  constructor(readonly text: string) {}

  get isInt(): boolean {
    return !/[.eE]/.test(this.text);
  }

  get value(): number {
    return Number(this.text);
  }
}

/** A JSON object; a Map keeps insertion order even for integer-like keys. */
export class PyObject extends Map<string, PyValue> {}

export class PyJsonError extends Error {}

// Sticky patterns match in place, so parsing stays linear in the body size.
const STRING = /"(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"/y;
const NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;

/** Parse JSON text, refusing what Python's json.loads would refuse (plus NaN/Infinity). */
export function parse(text: string): PyValue {
  let at = 0;
  const fail = (what: string): never => {
    throw new PyJsonError(`${what} at position ${at}`);
  };
  const space = () => {
    while (at < text.length && " \t\n\r".includes(text[at])) at++;
  };
  const value = (): PyValue => {
    space();
    const char = text[at];
    if (char === "{") return object();
    if (char === "[") return array();
    if (char === '"') return string();
    if (text.startsWith("true", at)) return (at += 4), true;
    if (text.startsWith("false", at)) return (at += 5), false;
    if (text.startsWith("null", at)) return (at += 4), null;
    return number();
  };
  const object = (): PyObject => {
    const out = new PyObject();
    at++;
    space();
    if (text[at] === "}") return at++, out;
    for (;;) {
      space();
      if (text[at] !== '"') fail("expected a string key");
      const key = string();
      space();
      if (text[at] !== ":") fail("expected ':'");
      at++;
      out.set(key, value());
      space();
      if (text[at] === ",") {
        at++;
        continue;
      }
      if (text[at] === "}") return at++, out;
      fail("expected ',' or '}'");
    }
  };
  const array = (): PyValue[] => {
    const out: PyValue[] = [];
    at++;
    space();
    if (text[at] === "]") return at++, out;
    for (;;) {
      out.push(value());
      space();
      if (text[at] === ",") {
        at++;
        continue;
      }
      if (text[at] === "]") return at++, out;
      fail("expected ',' or ']'");
    }
  };
  const string = (): string => {
    STRING.lastIndex = at;
    const match = STRING.exec(text);
    if (!match) fail("invalid string");
    at += match![0].length;
    return JSON.parse(match![0]) as string;
  };
  const number = (): PyNumber => {
    NUMBER.lastIndex = at;
    const match = NUMBER.exec(text);
    if (!match) fail("expected a value");
    at += match![0].length;
    return new PyNumber(match![0]);
  };
  const result = value();
  space();
  if (at !== text.length) fail("extra data");
  return result;
}

/** Python's repr() of a float, which json.dumps writes for every float. */
export function floatRepr(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  // JS and Python both print the shortest round-tripping digits; only the layout differs.
  const [mantissa, exponentText] = Math.abs(value).toExponential().split("e");
  const digits = mantissa.replace(".", "");
  const exponent = Number(exponentText);
  const sign = value < 0 ? "-" : "";
  if (exponent < -4 || exponent >= 16) {
    const fraction = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const magnitude = String(Math.abs(exponent)).padStart(2, "0");
    return `${sign}${fraction}e${exponent < 0 ? "-" : "+"}${magnitude}`;
  }
  if (exponent < 0) return `${sign}0.${"0".repeat(-exponent - 1)}${digits}`;
  const whole = digits.slice(0, exponent + 1).padEnd(exponent + 1, "0");
  const fraction = digits.slice(exponent + 1) || "0";
  return `${sign}${whole}.${fraction}`;
}

function number(value: PyNumber): string {
  if (value.isInt) return BigInt(value.text).toString();
  return floatRepr(value.value);
}

// ensure_ascii=False escapes only quotes, backslashes and control characters.
const ESCAPES: Record<string, string> = { '"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f" };

function quote(text: string): string {
  return `"${text.replace(/["\\\u0000-\u001f]/g, (char) => ESCAPES[char] ?? `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`)}"`;
}

/** Python sorts keys by code point; JS sorts by UTF-16 unit, which differs past U+FFFF. */
function byCodePoint(a: string, b: string): number {
  const left = Array.from(a, (char) => char.codePointAt(0)!);
  const right = Array.from(b, (char) => char.codePointAt(0)!);
  for (let index = 0; index < Math.min(left.length, right.length); index++) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return left.length - right.length;
}

/** `json.dumps(value, ensure_ascii=False, sort_keys=sortKeys, separators=(",", ":"))`. */
export function encode(value: PyValue, sortKeys = false): string {
  if (value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "string") return quote(value);
  if (value instanceof PyNumber) return number(value);
  if (Array.isArray(value)) return `[${value.map((item) => encode(item, sortKeys)).join(",")}]`;
  const keys = [...value.keys()];
  if (sortKeys) keys.sort(byCodePoint);
  return `{${keys.map((key) => `${quote(key)}:${encode(value.get(key)!, sortKeys)}`).join(",")}}`;
}
