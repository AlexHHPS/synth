import CoreML
import Foundation
import PDFKit
import Vision

enum MediaText {
    private enum ReadError: Error { case invalidPDF }

    static func read(_ path: String) throws -> String {
        let url = URL(fileURLWithPath: path)
        if url.pathExtension.lowercased() == "pdf" {
            guard let pdf = PDFDocument(url: url), !pdf.isLocked else { throw ReadError.invalidPDF }
            let pages = (0..<min(pdf.pageCount, 12)).compactMap { pdf.page(at: $0)?.string }
            return String(pages.joined(separator: "\n").prefix(1500))
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["es-ES", "en-US"]
        // The background host can run without an interactive Neural Engine service.
        for (stage, devices) in try request.supportedComputeStageDevices {
            if let cpu = devices.first(where: { device in
                if case .cpu = device { return true }
                return false
            }) {
                request.setComputeDevice(cpu, for: stage)
            }
        }
        let handler = VNImageRequestHandler(url: url)
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        return String(lines.joined(separator: "\n").prefix(1500))
    }
}
