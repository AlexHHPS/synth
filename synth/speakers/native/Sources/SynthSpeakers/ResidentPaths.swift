import Darwin
import Foundation

func residentCanonicalPath(_ path: String) throws -> String {
    guard let resolved = realpath(path, nil) else { throw ResidentError.path }
    defer { free(resolved) }
    return String(cString: resolved)
}

/// Foundation standardizes an existing /private/var file to /var, but keeps
/// /private/var for a not-yet-created file. Compare POSIX parents consistently.
func residentLocation(_ value: Any?, directory: URL) throws -> URL {
    guard let path = value as? String, path.utf8.count <= 4096,
          path.hasPrefix("/"), !path.contains("\0") else { throw ResidentError.path }
    let url = URL(fileURLWithPath: path)
    let parent = try residentCanonicalPath(url.deletingLastPathComponent().path)
    let canonical = parent + "/" + url.lastPathComponent
    guard path == canonical, path.hasPrefix(directory.path + "/") else { throw ResidentError.path }
    if FileManager.default.fileExists(atPath: path) {
        guard try residentCanonicalPath(path) == path else { throw ResidentError.path }
    } else if (try? FileManager.default.destinationOfSymbolicLink(atPath: path)) != nil {
        throw ResidentError.path
    }
    return url
}
