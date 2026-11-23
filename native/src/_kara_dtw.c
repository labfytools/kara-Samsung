#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "whisper.h"

static float *read_pcm(int *length)
{
    if (fseek(stdin, 0, SEEK_END) != 0)
        return NULL;
    long bytes = ftell(stdin);
    if (bytes < 2 || bytes % 2 != 0 || bytes / 2 > INT_MAX ||
        fseek(stdin, 0, SEEK_SET) != 0)
        return NULL;
    *length = (int)(bytes / 2);
    float *samples = malloc((size_t)*length * sizeof(*samples));
    if (!samples)
        return NULL;
    int16_t block[4096];
    int position = 0;
    size_t count;
    while ((count = fread(block, sizeof(*block), 4096, stdin)) > 0) {
        for (size_t i = 0; i < count; i++)
            samples[position++] = block[i] / 32768.0f;
    }
    if (position != *length) {
        free(samples);
        return NULL;
    }
    return samples;
}

static void print_tokens(struct whisper_context *context, int length_cs, bool live)
{
    const int end_token = whisper_token_eot(context);
    for (int segment = 0; segment < whisper_full_n_segments(context); segment++) {
        int64_t end = whisper_full_get_segment_t1(context, segment);
        if (end > length_cs + 55 && !live)
            continue;
        if (live && end > length_cs)
            end = length_cs;
        printf("segment %" PRId64 "\n", end);
        for (int i = 0; i < whisper_full_n_tokens(context, segment); i++) {
            whisper_token_data token = whisper_full_get_token_data(context, segment, i);
            if (token.id >= end_token || token.t_dtw < 0 || (!live && token.t_dtw > length_cs + 10))
                continue;
            const unsigned char *text = (const unsigned char *)whisper_full_get_token_text(context, segment, i);
            if (text == NULL || *text == '\0')
                continue;
            printf("token %" PRId64 " ", token.t_dtw);
            for (; *text; text++)
                printf("%02x", *text);
            putchar('\n');
        }
    }
}

static struct whisper_context *open_model(const char *model)
{
    struct whisper_context_params config = whisper_context_default_params();
    config.flash_attn = false;
    config.dtw_token_timestamps = true;
    config.dtw_aheads_preset = WHISPER_AHEADS_LARGE_V3_TURBO;
    struct whisper_context *context = whisper_init_from_file_with_params(model, config);
    return context;
}

static struct whisper_full_params decode_options(const char *language)
{
    struct whisper_full_params params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    params.language = language;
    params.n_threads = 4;
    params.no_context = true;
    params.print_progress = false;
    params.print_realtime = false;
    params.print_special = false;
    params.print_timestamps = false;
    params.no_speech_thold = .65f;
    params.temperature_inc = 0.0f;
    params.audio_ctx = 900;
    return params;
}

static int aligned_file(int argc, char **argv)
{
    if (argc != 3)
        return 2;
    int length = 0;
    float *audio = read_pcm(&length);
    if (!audio)
        return 3;
    struct whisper_context *context = open_model(argv[1]);
    if (!context) {
        free(audio);
        return 4;
    }
    struct whisper_full_params params = decode_options(argv[2]);
    const int stride = 12 * WHISPER_SAMPLE_RATE;
    const int pad = 2 * WHISPER_SAMPLE_RATE;
    int result = 0;
    for (int low = 0; low < length; low += stride) {
        int high = length - low >= stride ? low + stride : length;
        int start = low > pad ? low - pad : 0;
        int stop = length - high >= pad ? high + pad : length;
        if (stop - start < WHISPER_SAMPLE_RATE)
            continue;
        if (whisper_full(context, params, audio + start, stop - start) != 0) {
            result = 5;
            break;
        }
        printf("window %d %d %d\n", start / 160, low / 160, high / 160);
        print_tokens(context, (stop - start) / 160, false);
        printf("progress %d\n", (int)(100.0 * high / length));
        fflush(stdout);
    }
    whisper_free(context);
    free(audio);
    return result;
}


static int stream_audio(const char *model, const char *language, bool live)
{
    enum { MAX_SAMPLES = 18 * WHISPER_SAMPLE_RATE, MAX_PROMPT = 2048 };
    struct whisper_context *context = open_model(model);
    if (!context)
        return 4;
    int16_t *pcm = malloc(MAX_SAMPLES * sizeof(*pcm));
    float *audio = malloc(MAX_SAMPLES * sizeof(*audio));
    if (!pcm || !audio) {
        free(audio);
        free(pcm);
        whisper_free(context);
        return 3;
    }
    struct whisper_full_params params = decode_options(language);
    if (live)
        params.no_speech_thold = .83f;
    puts("ready");
    fflush(stdout);
    uint32_t header[2];
    char prompt[MAX_PROMPT + 1];
    int result = 0;
    while (fread(header, sizeof(*header), 2, stdin) == 2) {
        uint32_t count = header[0];
        uint32_t prompt_len = header[1];
        if (count == 0 || count > MAX_SAMPLES || prompt_len > MAX_PROMPT ||
            fread(prompt, 1, prompt_len, stdin) != prompt_len ||
            fread(pcm, sizeof(*pcm), count, stdin) != count) {
            result = 6;
            break;
        }
        prompt[prompt_len] = '\0';
        params.initial_prompt = prompt_len ? prompt : NULL;
        params.audio_ctx = count <= 11 * WHISPER_SAMPLE_RATE ? 640 : 960;
        for (uint32_t index = 0; index < count; index++)
            audio[index] = pcm[index] / 32768.0f;
        int duration = (int)(count / 160);
        if (live)
            params.max_tokens = 16 + duration / 10;
        if (whisper_full(context, params, audio, (int)count) == 0) {
            printf("window 0 0 %d\n", duration);
            print_tokens(context, duration, live);
        } else {
            puts("error");
        }
        puts("done");
        fflush(stdout);
    }
    free(audio);
    free(pcm);
    whisper_free(context);
    return result;
}

int main(int argc, char **argv)
{
    if (argc == 4 && strcmp(argv[1], "--stream") == 0)
        return stream_audio(argv[2], argv[3], false);
    if (argc == 4 && strcmp(argv[1], "--live") == 0)
        return stream_audio(argv[2], argv[3], true);
    return aligned_file(argc, argv);
}
