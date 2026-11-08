#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use log;
use env_logger;

fn main() {
    std::env::set_var("RUST_LOG", "info");
    env_logger::init();

    let arguments: Vec<String> = std::env::args().collect();
    if arguments.get(1).map(String::as_str) == Some("--verify-qwen-audio") {
        if arguments.len() != 4 { eprintln!("Expected app-data directory and a public audio fixture"); std::process::exit(2); }
        let resources = std::env::current_exe().unwrap().parent().unwrap().parent().unwrap().join("Resources");
        match app_lib::audio::transcription::qwen_provider::verify_audio_path(
            arguments[2].clone().into(), resources, arguments[3].clone().into()) {
            Ok(result) => println!("{}",result),
            Err(error) => { eprintln!("{}",error); std::process::exit(1); }
        }
        return;
    }

    // Async logger will be initialized lazily when first needed (after Tauri runtime starts)
    log::info!("Starting application...");
    app_lib::run();
}
