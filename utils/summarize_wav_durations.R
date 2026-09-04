#!/usr/bin/env Rscript

# Summarize durations of ordinary PCM WAV files below one directory.
# The reader stops at the WAV data-chunk header; it never reads audio samples.

read_exact <- function(connection, count, description) {
  bytes <- readBin(connection, what = "raw", n = count)
  if (length(bytes) != count) {
    stop(sprintf("incomplete %s", description), call. = FALSE)
  }
  bytes
}

unsigned_16_le <- function(bytes, offset = 1L) {
  values <- as.numeric(bytes[offset + 0:1])
  values[1] + values[2] * 256
}

unsigned_32_le <- function(bytes, offset = 1L) {
  values <- as.numeric(bytes[offset + 0:3])
  sum(values * 256^(0:3))
}

wav_duration <- function(path) {
  connection <- file(path, open = "rb")
  on.exit(close(connection))

  riff_id <- rawToChar(read_exact(connection, 4, "RIFF identifier"))
  read_exact(connection, 4, "RIFF size")
  wave_id <- rawToChar(read_exact(connection, 4, "WAVE identifier"))
  if (riff_id != "RIFF" || wave_id != "WAVE") {
    stop("not a RIFF/WAVE file", call. = FALSE)
  }

  format <- NULL
  data_bytes <- NULL

  repeat {
    chunk_id <- rawToChar(read_exact(connection, 4, "chunk identifier"))
    chunk_size <- unsigned_32_le(read_exact(connection, 4, "chunk size"))
    padding <- chunk_size %% 2

    if (chunk_id == "fmt ") {
      if (chunk_size < 16) {
        stop("WAV format chunk is shorter than 16 bytes", call. = FALSE)
      }

      bytes <- read_exact(connection, 16, "WAV format data")
      format <- list(
        type = unsigned_16_le(bytes, 1),
        channels = unsigned_16_le(bytes, 3),
        sample_rate = unsigned_32_le(bytes, 5),
        byte_rate = unsigned_32_le(bytes, 9),
        block_alignment = unsigned_16_le(bytes, 13),
        bits_per_sample = unsigned_16_le(bytes, 15)
      )

      remaining <- chunk_size - 16 + padding
      if (remaining > 0) {
        seek(connection, where = remaining, origin = "current")
      }
    } else if (chunk_id == "data") {
      data_bytes <- chunk_size
      break
    } else {
      seek(connection, where = chunk_size + padding, origin = "current")
    }
  }

  if (is.null(format)) {
    stop("WAV has no format chunk before its data chunk", call. = FALSE)
  }
  if (format$type != 1) {
    stop(sprintf("unsupported WAV format type: %s", format$type), call. = FALSE)
  }
  if (format$sample_rate <= 0 || format$block_alignment <= 0) {
    stop("invalid sample rate or block alignment", call. = FALSE)
  }
  if (format$byte_rate != format$sample_rate * format$block_alignment) {
    stop("inconsistent byte rate in WAV header", call. = FALSE)
  }
  if (data_bytes <= 0 || data_bytes %% format$block_alignment != 0) {
    stop("invalid audio-data size", call. = FALSE)
  }

  sample_frames <- data_bytes / format$block_alignment
  sample_frames / format$sample_rate
}

summarize_wavs <- function(root) {
  files <- list.files(
    root,
    pattern = "\\.wav$",
    recursive = TRUE,
    full.names = TRUE,
    ignore.case = TRUE
  )
  files <- files[!file.info(files)$isdir]

  total_seconds <- 0
  files_measured <- 0L
  failed_paths <- character()
  failed_messages <- character()

  for (path in files) {
    result <- tryCatch(wav_duration(path), error = identity)
    if (inherits(result, "error")) {
      failed_paths <- c(failed_paths, path)
      failed_messages <- c(failed_messages, conditionMessage(result))
      next
    }

    files_measured <- files_measured + 1L
    total_seconds <- total_seconds + result
    cat(sprintf("%12.3f seconds  %s\n", result, path))
  }

  cat("\n")
  cat(sprintf("WAV files found:       %s\n", format(length(files), big.mark = ",")))
  cat(sprintf("Successfully measured: %s\n", format(files_measured, big.mark = ",")))
  cat(sprintf("Could not be measured: %s\n", format(length(failed_paths), big.mark = ",")))
  cat(sprintf("Total seconds:         %s\n", format(round(total_seconds, 3), big.mark = ",", nsmall = 3)))
  cat(sprintf("Total minutes:         %s\n", format(round(total_seconds / 60, 2), big.mark = ",", nsmall = 2)))
  cat(sprintf("Total hours:           %s\n", format(round(total_seconds / 3600, 2), big.mark = ",", nsmall = 2)))

  if (length(failed_paths) > 0) {
    cat("\nFiles requiring review:\n")
    for (index in seq_along(failed_paths)) {
      cat(sprintf("  %s: %s\n", failed_paths[index], failed_messages[index]))
    }
  }
}

arguments <- commandArgs(trailingOnly = TRUE)
if (length(arguments) != 1) {
  stop("Usage: summarize_wav_durations.R /path/to/wav/catalog", call. = FALSE)
}

root <- normalizePath(arguments[1], mustWork = TRUE)
summarize_wavs(root)
