// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "SynthSpeakers",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "synth-speakers", targets: ["SynthSpeakers"])],
    dependencies: [
        .package(url: "https://github.com/FluidInference/FluidAudio.git",
                 revision: "c388107348134698135cfd34f3f59dc823b6e7ce")
    ],
    targets: [.executableTarget(name: "SynthSpeakers", dependencies: [
        .product(name: "FluidAudio", package: "FluidAudio")
    ])]
)
