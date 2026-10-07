import { describe, expect, it } from "vitest";
import { qrMatrix } from "./qrCode";

const draw = (matrix: boolean[][]) => matrix.map(row => row.map(dark => (dark ? "#" : ".")).join(""));

describe("QR encoding", () => {
  it("matches a symbol verified with a QR decoder", () => {
    // Decoded as "LearningTree" by macOS CoreImage before being recorded here.
    expect(draw(qrMatrix("LearningTree"))).toEqual([
      "#######..##...#######",
      "#.....#..###..#.....#",
      "#.###.#....#..#.###.#",
      "#.###.#...##..#.###.#",
      "#.###.#..#.##.#.###.#",
      "#.....#.##....#.....#",
      "#######.#.#.#.#######",
      ".....................",
      "#..#.##.#..###.#.....",
      "#....#.#.#.#...##..##",
      "###.###.##.#...#.##.#",
      "..####.####.#.####.##",
      ".##..##..###.###...#.",
      "........##.#.#.#.#...",
      "#######....##..##.##.",
      "#.....#.###........#.",
      "#.###.#..#.##...##...",
      "#.###.#.###.#####..##",
      "#.###.#..#.##..##...#",
      "#.....#..#...#.#.....",
      "#######.###.#.##.#.#.",
    ]);
  });

  it("uses the smallest version that fits, up to version 10", () => {
    expect(qrMatrix("https://study-macbook-air.tail1a2b3c.ts.net")).toHaveLength(33);
    expect(qrMatrix("x".repeat(62))).toHaveLength(33);
    expect(qrMatrix("x".repeat(63))).toHaveLength(37);
    expect(qrMatrix("x".repeat(213))).toHaveLength(57);
    expect(() => qrMatrix("x".repeat(214))).toThrow(RangeError);
  });

  it("places finder patterns in three corners", () => {
    const matrix = qrMatrix("https://mac.tail0.ts.net");
    const size = matrix.length;
    const finder = (x0: number, y0: number) => draw(matrix.slice(y0, y0 + 7).map(row => row.slice(x0, x0 + 7)));
    const expected = ["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"];
    expect([finder(0, 0), finder(size - 7, 0), finder(0, size - 7)]).toEqual([expected, expected, expected]);
  });
});
