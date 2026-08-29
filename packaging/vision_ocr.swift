import AppKit
import Foundation
import PDFKit
import Vision

struct Options {
    var boxes = false
    var page = 1
    var maxPages = 5
    var dpi = 150
    var input = ""
}

enum OCRError: Error, CustomStringConvertible {
    case usage(String)
    case input(String)

    var description: String {
        switch self {
        case .usage(let message), .input(let message): return message
        }
    }
}

func parseOptions() throws -> Options {
    var options = Options()
    let args = Array(CommandLine.arguments.dropFirst())
    var index = 0
    while index < args.count {
        let arg = args[index]
        switch arg {
        case "--boxes":
            options.boxes = true
        case "--page", "--max-pages", "--dpi":
            index += 1
            guard index < args.count, let value = Int(args[index]), value > 0 else {
                throw OCRError.usage("\(arg) requires a positive integer")
            }
            if arg == "--page" { options.page = value }
            if arg == "--max-pages" { options.maxPages = value }
            if arg == "--dpi" { options.dpi = value }
        default:
            if arg.hasPrefix("-") || !options.input.isEmpty {
                throw OCRError.usage("unknown or duplicate argument: \(arg)")
            }
            options.input = arg
        }
        index += 1
    }
    guard !options.input.isEmpty else {
        throw OCRError.usage("usage: ainote-vision-ocr [--boxes] [--page N] [--max-pages N] [--dpi N] INPUT")
    }
    return options
}

func render(_ page: PDFPage, dpi: Int) throws -> CGImage {
    let bounds = page.bounds(for: .mediaBox)
    let scale = CGFloat(dpi) / 72.0
    let width = max(1, Int(ceil(bounds.width * scale)))
    let height = max(1, Int(ceil(bounds.height * scale)))
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        throw OCRError.input("cannot create PDF render context")
    }
    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    context.translateBy(x: -bounds.minX, y: -bounds.minY)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()
    guard let image = context.makeImage() else {
        throw OCRError.input("cannot render PDF page")
    }
    return image
}

func loadImages(_ options: Options) throws -> [CGImage] {
    let url = URL(fileURLWithPath: options.input)
    guard FileManager.default.fileExists(atPath: url.path) else {
        throw OCRError.input("input file does not exist")
    }
    if url.pathExtension.lowercased() == "pdf" {
        guard let document = PDFDocument(url: url), document.pageCount > 0 else {
            throw OCRError.input("cannot open PDF")
        }
        if options.boxes {
            guard options.page <= document.pageCount,
                  let pdfPage = document.page(at: options.page - 1) else {
                throw OCRError.input("PDF page is out of range")
            }
            return [try render(pdfPage, dpi: options.dpi)]
        }
        let count = min(options.maxPages, document.pageCount)
        return try (0..<count).map { index in
            guard let pdfPage = document.page(at: index) else {
                throw OCRError.input("cannot load PDF page \(index + 1)")
            }
            return try render(pdfPage, dpi: options.dpi)
        }
    }
    guard let source = NSImage(contentsOf: url),
          let image = source.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        throw OCRError.input("cannot load image")
    }
    return [image]
}

func recognize(_ image: CGImage) throws -> [VNRecognizedTextObservation] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ja-JP", "en-US"]
    request.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return request.results ?? []
}

do {
    let options = try parseOptions()
    let pages = try loadImages(options)
    if options.boxes {
        var output: [[String: Any]] = []
        for image in pages {
            for observation in try recognize(image) {
                guard let candidate = observation.topCandidates(1).first else { continue }
                let box = observation.boundingBox
                output.append([
                    "text": candidate.string,
                    "x": box.origin.x,
                    "y": box.origin.y,
                    "w": box.size.width,
                    "h": box.size.height,
                ])
            }
        }
        let data = try JSONSerialization.data(withJSONObject: output)
        print(String(data: data, encoding: .utf8) ?? "[]")
    } else {
        var lines: [String] = []
        for image in pages {
            lines.append(contentsOf: try recognize(image).compactMap { $0.topCandidates(1).first?.string })
        }
        print(lines.joined(separator: "\n"))
    }
} catch {
    FileHandle.standardError.write(Data("ainote-vision-ocr: \(error)\n".utf8))
    exit(1)
}
