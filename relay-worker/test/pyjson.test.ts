/** Python's json encoding, checked against outputs of CPython's json.dumps. */

import { describe, expect, test } from "vitest";
import { PyJsonError, encode, floatRepr, parse } from "../src/pyjson";

describe("floatRepr writes floats as json.dumps(float(x))", () => {
  test.each([
    ["1767225600.0", "1767225600.0"],
    ["0.1", "0.1"],
    ["1e16", "1e+16"],
    ["1e-05", "1e-05"],
    ["3.0", "3.0"],
    ["1e15", "1000000000000000.0"],
    ["0.0001", "0.0001"],
    ["123456789.123", "123456789.123"],
    ["-2.5", "-2.5"],
    ["1e22", "1e+22"],
    ["5e-324", "5e-324"],
    ["1.7976931348623157e308", "1.7976931348623157e+308"],
    ["-0.0", "-0.0"],
    ["100.0", "100.0"],
    ["1234567890123456.7", "1234567890123456.8"],
    ["12.5e3", "12500.0"],
    ["0.000123", "0.000123"],
  ])("%s", (lexeme, python) => {
    expect(floatRepr(Number(lexeme))).toBe(python);
  });
});

// json.loads of SOURCE, re-encoded by CPython with and without sort_keys.
const SOURCE = '{"b":1.0,"a":[1,2.50,1e16,1E-5,"é\\n\\u0001"],"10":{"z":null,"\u{1F680}":true,"￿":false},"b":-0,"big":123456789012345678901234567890}';

describe("encode matches json.dumps(json.loads(SOURCE), ensure_ascii=False, separators=(',', ':'))", () => {
  test("with sort_keys, by code point", () => {
    expect(encode(parse(SOURCE), true)).toBe('{"10":{"z":null,"￿":false,"\u{1F680}":true},"a":[1,2.5,1e+16,1e-05,"é\\n\\u0001"],"b":0,"big":123456789012345678901234567890}');
  });

  test("in insertion order, a repeated key keeping its first place and last value", () => {
    expect(encode(parse(SOURCE))).toBe('{"b":0,"a":[1,2.5,1e+16,1e-05,"é\\n\\u0001"],"10":{"z":null,"\u{1F680}":true,"￿":false},"big":123456789012345678901234567890}');
  });
});

describe("parse refuses what json.loads refuses", () => {
  test.each([["{"], ['{"a":1,}'], ["[01]"], ['"\u0001"'], ["{} x"], [""], ["'a'"]])("%j", (text) => {
    expect(() => parse(text)).toThrow(PyJsonError);
  });
});
