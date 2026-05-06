#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bilibili_whisper_transcribe.sh <video-url> [temp-parent-dir]

Environment variables:
  WHISPER_MODEL       Optional whisper model path passed via -m
  WHISPER_LANG        Whisper language, defaults to auto
  WHISPER_BEST_OF     Whisper best-of candidates, defaults to 1
  WHISPER_BEAM_SIZE   Whisper beam size, defaults to 1
  WHISPER_EXTRA_ARGS  Extra flags appended to whisper-cli
  YTDLP_EXTRA_ARGS    Extra flags appended to yt-dlp
  FFMPEG_EXTRA_ARGS   Extra flags appended to ffmpeg
  YTDLP_COOKIES_FROM_BROWSER Optional browser cookie source, e.g. chrome:Profile 1

Outputs:
  <output-dir>/<video-id-or-timestamp>/
    source.*          Downloaded media
    source.info.json   yt-dlp metadata when available
    audio.wav         Mono 16k wav for Whisper
    transcript.txt
    transcript.srt
    transcript.json
EOF
}

detect_platform() {
  local url="$1"
  local lowered
  lowered="$(printf '%s' "$url" | tr '[:upper:]' '[:lower:]')"
  if [[ "$lowered" == *"youtube.com"* || "$lowered" == *"youtu.be"* ]]; then
    printf '%s\n' "youtube"
  elif [[ "$lowered" == *"bilibili.com"* || "$lowered" == *"b23.tv"* ]]; then
    printf '%s\n' "bilibili"
  else
    printf '%s\n' "video"
  fi
}

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

format_hhmmss() {
  local total="$1"
  local hours=$(( total / 3600 ))
  local minutes=$(((total % 3600) / 60))
  local seconds=$(( total % 60 ))
  printf '%02d:%02d:%02d' "$hours" "$minutes" "$seconds"
}

is_tty() {
  [[ -t 2 ]]
}

probe_duration_seconds() {
  local file="$1"
  if command -v ffprobe >/dev/null 2>&1; then
    local duration
    duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" 2>/dev/null || true)"
    if [[ -n "$duration" ]]; then
      awk -v dur="$duration" 'BEGIN { if (dur > 0) printf "%d\n", dur + 0.5; }'
      return 0
    fi
  fi
  return 1
}

start_progress_bar() {
  local label="$1"
  local total_seconds="$2"
  local start_ts
  local progress_pid
  local bar_width=24

  if [[ -z "$total_seconds" || "$total_seconds" -le 0 ]]; then
    printf '%s\n' "$label" >&2
    return 0
  fi

  start_ts="$(date +%s)"
  (
    trap 'exit 0' INT TERM
    printf '%s\n' "$label" >&2
    while true; do
      local now elapsed percent filled empty bar gap elapsed_str total_str
      now="$(date +%s)"
      elapsed=$(( now - start_ts ))
      percent=$(( elapsed * 100 / total_seconds ))
      if (( percent > 99 )); then
        percent=99
      fi
      filled=$(( percent * bar_width / 100 ))
      empty=$(( bar_width - filled ))
      bar="$(printf '%*s' "$filled" '' | tr ' ' '=')"
      gap="$(printf '%*s' "$empty" '' | tr ' ' '.')"
      elapsed_str="$(format_hhmmss "$elapsed")"
      total_str="$(format_hhmmss "$total_seconds")"
      printf '%s [%s%s] %3d%% %s/%s\n' "$label" "$bar" "$gap" "$percent" "$elapsed_str" "$total_str" >&2
      sleep 15
    done
  ) &
  progress_pid=$!
  printf '%s\n' "$progress_pid"
}

start_progress_log() {
  local label="$1"
  local total_seconds="$2"
  local start_ts
  local progress_pid

  if [[ -z "$total_seconds" || "$total_seconds" -le 0 ]]; then
    printf '%s\n' "$label" >&2
    return 0
  fi

  start_ts="$(date +%s)"
  (
    trap 'exit 0' INT TERM
    printf '%s\n' "$label" >&2
    while true; do
      local now elapsed percent elapsed_str total_str
      now="$(date +%s)"
      elapsed=$(( now - start_ts ))
      percent=$(( elapsed * 100 / total_seconds ))
      if (( percent > 99 )); then
        percent=99
      fi
      elapsed_str="$(format_hhmmss "$elapsed")"
      total_str="$(format_hhmmss "$total_seconds")"
      printf '%s: %3d%% (%s/%s)\n' "$label" "$percent" "$elapsed_str" "$total_str" >&2
      sleep 15
    done
  ) &
  progress_pid=$!
  printf '%s\n' "$progress_pid"
}

stop_progress_bar() {
  local progress_pid="${1:-}"
  if [[ -n "$progress_pid" ]]; then
    kill "$progress_pid" >/dev/null 2>&1 || true
    wait "$progress_pid" >/dev/null 2>&1 || true
  fi
}

append_split_words() {
  local raw="$1"
  local ref_name="$2"
  if [[ -n "$raw" ]]; then
    # Intentional word splitting for flag expansion.
    # shellcheck disable=SC2206
    local extra=( $raw )
    local item
    for item in "${extra[@]}"; do
      eval "$ref_name+=(\"\$item\")"
    done
  fi
}

resolve_whisper_model() {
  if [[ -n "${WHISPER_MODEL:-}" ]]; then
    if [[ -f "$WHISPER_MODEL" ]]; then
      printf '%s\n' "$WHISPER_MODEL"
      return 0
    fi
    echo "Configured WHISPER_MODEL does not exist: $WHISPER_MODEL" >&2
    exit 1
  fi

  local candidates=(
    "$HOME/.whisper/ggml-large-v2.bin"
    "$HOME/.whisper/ggml-large.bin"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "No Whisper model found. Set WHISPER_MODEL or place a large model under ~/.whisper/." >&2
  exit 1
}

max_segment_seconds() {
  local default_seconds="${TRANSCRIBE_MAX_SEGMENT_SECONDS:-2400}"
  if [[ "$default_seconds" =~ ^[0-9]+$ ]] && [[ "$default_seconds" -gt 0 ]]; then
    printf '%s\n' "$default_seconds"
  else
    printf '%s\n' 2400
  fi
}

split_audio_segments() {
  local audio_path="$1"
  local segment_dir="$2"
  local segment_seconds="$3"
  mkdir -p "$segment_dir"
  ffmpeg -hide_banner -loglevel error -y \
    -i "$audio_path" \
    -map 0:a:0 \
    -c:a pcm_s16le \
    -f segment \
    -segment_time "$segment_seconds" \
    -reset_timestamps 1 \
    "$segment_dir/segment_%03d.wav"
}

run_whisper_cli() {
  local input_path="$1"
  local output_prefix="$2"
  local label="$3"
  local whisper_model="$4"
  local whisper_lang="$5"
  local whisper_best_of="$6"
  local whisper_beam_size="$7"

  local audio_duration_seconds
  audio_duration_seconds="$(probe_duration_seconds "$input_path" || true)"
  local progress_pid=""
  if is_tty; then
    progress_pid="$(start_progress_bar "$label" "${audio_duration_seconds:-}")"
  else
    progress_pid="$(start_progress_log "$label" "${audio_duration_seconds:-}")"
  fi
  local whisper_args=(
    --language "$whisper_lang"
    --best-of "$whisper_best_of"
    --beam-size "$whisper_beam_size"
    --output-txt
    --output-srt
    --output-json
    --output-file "$output_prefix"
    -m "$whisper_model"
  )
  append_split_words "${WHISPER_EXTRA_ARGS:-}" whisper_args
  whisper_args+=("$input_path")

  echo "Running whisper-cli with model $whisper_model" >&2
  if ! whisper-cli "${whisper_args[@]}"; then
    stop_progress_bar "$progress_pid"
    return 1
  fi
  stop_progress_bar "$progress_pid"
  return 0
}

combine_segment_transcripts() {
  local combined_txt="$1"
  shift
  local segments=("$@")
  : > "$combined_txt"
  local total="${#segments[@]}"
  local idx=0
  local segment_prefix
  for segment_prefix in "${segments[@]}"; do
    idx=$((idx + 1))
    local segment_txt="${segment_prefix}.txt"
    if [[ ! -f "$segment_txt" ]]; then
      continue
    fi
    {
      printf '\n## Segment %d/%d\n\n' "$idx" "$total"
      cat "$segment_txt"
      printf '\n'
    } >> "$combined_txt"
  done
}

remove_segment_audio_files() {
  local segment_dir="$1"
  if [[ -d "$segment_dir" ]]; then
    find "$segment_dir" -type f -name 'segment_*.wav' -delete
    rmdir "$segment_dir" 2>/dev/null || true
  fi
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
    usage
    exit 0
  fi

  require_bin yt-dlp
  require_bin ffmpeg
  require_bin whisper-cli

  local url="$1"
  local temp_root="${2:-${WORK_ROOT:-$PWD/tmp}}"
  local run_id
  local work_dir
  local source_base
  local downloaded_path
  local audio_path
  local transcript_base
  local whisper_model
  local whisper_lang="${WHISPER_LANG:-auto}"
  local whisper_best_of="${WHISPER_BEST_OF:-1}"
  local whisper_beam_size="${WHISPER_BEAM_SIZE:-1}"
  local segment_seconds
  local audio_duration_seconds
  local split_mode=0
  local platform
  platform="$(detect_platform "$url")"

  mkdir -p "$temp_root"
  work_dir="$(mktemp -d "${temp_root%/}/video-whisper.XXXXXX")"

  source_base="$work_dir/source.%(ext)s"
  audio_path="$work_dir/audio.wav"
  transcript_base="$work_dir/transcript"

  local yt_args=(
    --no-playlist
    --restrict-filenames
    --write-info-json
    -f "bv*+ba/b"
    --merge-output-format mp4
    -o "$source_base"
  )
  if [[ "$platform" == "bilibili" ]]; then
    yt_args+=(
      --add-headers "Referer:https://www.bilibili.com"
      --add-headers "Origin:https://www.bilibili.com"
    )
  fi
  if [[ -n "${YTDLP_COOKIES_FROM_BROWSER:-}" ]]; then
    yt_args+=(--cookies-from-browser "$YTDLP_COOKIES_FROM_BROWSER")
  fi
  append_split_words "${YTDLP_EXTRA_ARGS:-}" yt_args
  yt_args+=("$url")

  echo "Downloading media into $work_dir" >&2
  yt-dlp "${yt_args[@]}"

  downloaded_path="$(find "$work_dir" -maxdepth 1 -type f -name 'source.*' ! -name 'source.info.json' | head -n 1)"
  if [[ -z "$downloaded_path" ]]; then
    echo "Failed to locate downloaded media in $work_dir" >&2
    exit 1
  fi

  local ffmpeg_args=(
    -y
    -i "$downloaded_path"
    -vn
    -ac 1
    -ar 16000
    "$audio_path"
  )
  append_split_words "${FFMPEG_EXTRA_ARGS:-}" ffmpeg_args

  echo "Extracting normalized audio to $audio_path" >&2
  ffmpeg "${ffmpeg_args[@]}"

  whisper_model="$(resolve_whisper_model)"
  audio_duration_seconds="$(probe_duration_seconds "$audio_path" || true)"
  segment_seconds="$(max_segment_seconds)"
  if [[ -n "$audio_duration_seconds" && "$audio_duration_seconds" -gt "$segment_seconds" ]]; then
    split_mode=1
  fi

  echo "Starting transcription with whisper-cli" >&2
  if [[ "$split_mode" -eq 1 ]]; then
    local segment_dir="$work_dir/segments"
    local segment_prefixes=()
    local segment_paths=()
    local segment_count=0
    local segment_path

    echo "Audio is longer than ${segment_seconds}s; splitting into segments before transcription" >&2
    split_audio_segments "$audio_path" "$segment_dir" "$segment_seconds"
    while IFS= read -r segment_path; do
      segment_paths+=("$segment_path")
    done < <(find "$segment_dir" -maxdepth 1 -type f -name 'segment_*.wav' | sort)
    segment_count="${#segment_paths[@]}"
    if [[ "$segment_count" -eq 0 ]]; then
      echo "Failed to split audio into segments in $segment_dir" >&2
      exit 1
    fi

    local idx=0
    for segment_path in "${segment_paths[@]}"; do
      idx=$((idx + 1))
      local segment_prefix="$work_dir/transcript.part$(printf '%03d' "$idx")"
      segment_prefixes+=("$segment_prefix")
      echo "Transcribing segment ${idx}/${segment_count}" >&2
      run_whisper_cli "$segment_path" "$segment_prefix" "Transcribing segment ${idx}/${segment_count} with whisper-cli" "$whisper_model" "$whisper_lang" "$whisper_best_of" "$whisper_beam_size"
      rm -f "$segment_path"
    done
    combine_segment_transcripts "$work_dir/transcript.txt" "${segment_prefixes[@]}"
    remove_segment_audio_files "$segment_dir"
  else
    echo "Transcribing single audio track" >&2
    run_whisper_cli "$audio_path" "$transcript_base" "Transcribing with whisper-cli" "$whisper_model" "$whisper_lang" "$whisper_best_of" "$whisper_beam_size"
  fi

  echo "$work_dir"
}

main "$@"
