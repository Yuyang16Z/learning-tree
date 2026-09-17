import AppKit

// Draw the existing green branch mark as resolution-independent macOS artwork.
func renderIcon(pixels: Int, destination: URL) throws {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ), let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw NSError(domain: "LearningTree.Icon", code: 1)
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let scale = CGFloat(pixels) / 1024
    context.cgContext.scaleBy(x: scale, y: scale)
    context.imageInterpolation = .high

    let tile = NSBezierPath(roundedRect: NSRect(x: 102, y: 106, width: 820, height: 820), xRadius: 182, yRadius: 182)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.23)
    shadow.shadowBlurRadius = 32
    shadow.shadowOffset = NSSize(width: 0, height: -15)
    shadow.set()
    NSColor(calibratedRed: 0.25, green: 0.65, blue: 0.52, alpha: 1).setFill()
    tile.fill()
    NSGraphicsContext.restoreGraphicsState()

    let gradient = NSGradient(colors: [
        NSColor(calibratedRed: 0.31, green: 0.73, blue: 0.59, alpha: 1),
        NSColor(calibratedRed: 0.43, green: 0.85, blue: 0.71, alpha: 1)
    ])!
    gradient.draw(in: tile, angle: 90)
    NSColor.white.withAlphaComponent(0.16).setStroke()
    tile.lineWidth = 2
    tile.stroke()

    let branch = NSBezierPath()
    branch.lineWidth = 42
    branch.lineCapStyle = .round
    branch.lineJoinStyle = .round
    branch.move(to: NSPoint(x: 330, y: 650))
    branch.line(to: NSPoint(x: 330, y: 494))
    branch.curve(to: NSPoint(x: 354, y: 470), controlPoint1: NSPoint(x: 330, y: 478), controlPoint2: NSPoint(x: 338, y: 470))
    branch.line(to: NSPoint(x: 670, y: 470))
    branch.curve(to: NSPoint(x: 694, y: 494), controlPoint1: NSPoint(x: 686, y: 470), controlPoint2: NSPoint(x: 694, y: 478))
    branch.line(to: NSPoint(x: 694, y: 650))
    branch.move(to: NSPoint(x: 512, y: 315))
    branch.line(to: NSPoint(x: 512, y: 650))
    NSColor(calibratedRed: 0.07, green: 0.22, blue: 0.18, alpha: 1).setStroke()
    branch.stroke()
    NSGraphicsContext.restoreGraphicsState()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "LearningTree.Icon", code: 2)
    }
    try png.write(to: destination)
}

do {
    guard CommandLine.arguments.count == 2 else {
        throw NSError(domain: "LearningTree.Icon", code: 3, userInfo: [NSLocalizedDescriptionKey: "Expected iconset output directory"])
    }
    let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
    for size in [16, 32, 128, 256, 512] {
        try renderIcon(pixels: size, destination: output.appendingPathComponent("icon_\(size)x\(size).png"))
        try renderIcon(pixels: size * 2, destination: output.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
    }
} catch {
    fputs("Icon generation failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
