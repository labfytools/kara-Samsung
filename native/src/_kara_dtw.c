#include <inttypes.h>
#include <limits.h>
#include <stdatomic.h>
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

static struct whisper_context *open_model(const char *model, bool gpu)
{
    struct whisper_context_params config = whisper_context_default_params();
    config.use_gpu = gpu;
    config.flash_attn = false;
    config.dtw_token_timestamps = true;
    config.dtw_aheads_preset = WHISPER_AHEADS_LARGE_V3_TURBO;
    struct whisper_context *context = whisper_init_from_file_with_params(model, config);
    return context;
}

struct vulkan_probe {
    atomic_bool selected;
    atomic_bool failed;
};

static void probe_vulkan(enum ggml_log_level level, const char *message, void *userdata)
{
    (void)level;
    struct vulkan_probe *probe = userdata;
    const char selected[] = "whisper_backend_init_gpu: using Vulkan";
    const char failed[] = "whisper_backend_init_gpu: failed to initialize";
    if (strncmp(message, selected, sizeof(selected) - 1) == 0)
        atomic_store(&probe->selected, true);
    if (strncmp(message, failed, sizeof(failed) - 1) == 0)
        atomic_store(&probe->failed, true);
    fputs(message, stderr);
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
    struct whisper_context *context = open_model(argv[1], true);
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
    struct whisper_context *context = open_model(model, true);
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

static void print_prepared(struct whisper_context *context)
{
    printf("language %s\n", whisper_lang_str(whisper_full_lang_id(context)));
    const int eot = whisper_token_eot(context);
    for (int i = 0; i < whisper_full_n_segments(context); i++) {
        printf("segment %" PRId64 " %" PRId64 " %.5f\n",
               whisper_full_get_segment_t0(context, i), whisper_full_get_segment_t1(context, i),
               whisper_full_get_segment_no_speech_prob(context, i));
        for (int j = 0; j < whisper_full_n_tokens(context, i); j++) {
            whisper_token_data token = whisper_full_get_token_data(context, i, j);
            const unsigned char *part = (const unsigned char *)whisper_full_get_token_text(context, i, j);
            if (token.id >= eot || part == NULL || *part == '\0')
                continue;
            printf("token %" PRId64 " %.5f ", token.t_dtw, token.p);
            for (; *part; part++)
                printf("%02x", *part);
            putchar('\n');
        }
    }
}

static int prepare_stream(const char *model, bool gpu, int threads, bool require_vulkan)
{
    enum { MAX_SAMPLES = 29 * WHISPER_SAMPLE_RATE, MAX_LANG = 15 };
    static struct vulkan_probe probe;
    /* Whisper may fall back to CPU after a GPU request. Its pinned backend
     * reports device selection and initialization failure before it is ready. */
    if (require_vulkan)
        whisper_log_set(probe_vulkan, &probe);
    struct whisper_context *context = open_model(model, gpu);
    if (require_vulkan)
        whisper_log_set(NULL, NULL);
    if (context && require_vulkan && (!atomic_load(&probe.selected) || atomic_load(&probe.failed))) {
        whisper_free(context);
        fputs("Kara: Vulkan backend unavailable\n", stderr);
        return 4;
    }
    float *samples = malloc(MAX_SAMPLES * sizeof(*samples));
    if (!context || !samples) {
        if (context)
            whisper_free(context);
        free(samples);
        return 4;
    }
    puts("ready");
    fflush(stdout);
    int status = 0;
    unsigned char header[5];
    while (true) {
        size_t size = fread(header, 1, sizeof(header), stdin);
        if (size == 0 && feof(stdin))
            break;
        if (size != sizeof(header)) {
            status = 6;
            break;
        }
        // Each request contains a little-endian PCM frame and a language code.
        // The model is process-owned and survives requests and language changes.
        uint32_t count = (uint32_t)header[0] | (uint32_t)header[1] << 8 |
                         (uint32_t)header[2] << 16 | (uint32_t)header[3] << 24;
        // The low bits retain the original language-length encoding.
        // The high bit selects beam search for an unprompted local retry.
        const bool beam = (header[4] & 0x80u) != 0;
        unsigned int lang_length = header[4] & 0x7fu;
        char language[MAX_LANG + 1];
        if (count == 0 || count > MAX_SAMPLES || lang_length == 0 || lang_length > MAX_LANG ||
            fread(language, 1, lang_length, stdin) != lang_length) {
            status = 6;
            break;
        }
        language[lang_length] = '\0';
        for (uint32_t offset = 0; offset < count;) {
            unsigned char pcm[8192];
            uint32_t batch = count - offset > sizeof(pcm) / 2 ? sizeof(pcm) / 2 : count - offset;
            if (fread(pcm, 2, batch, stdin) != batch) {
                status = 6;
                break;
            }
            for (uint32_t i = 0; i < batch; i++) {
                int value = pcm[2 * i] | (int)pcm[2 * i + 1] << 8;
                samples[offset + i] = (value >= 32768 ? value - 65536 : value) / 32768.0f;
            }
            offset += batch;
        }
        if (status)
            break;
        struct whisper_full_params params = decode_options(language);
        params.n_threads = threads;
        params.audio_ctx = 0;
        params.greedy.best_of = 1;
        if (beam) {
            params.strategy = WHISPER_SAMPLING_BEAM_SEARCH;
            params.beam_search.beam_size = 3;
        }
        if (whisper_full(context, params, samples, (int)count) == 0)
            print_prepared(context);
        else
            puts("error");
        puts("done");
        fflush(stdout);
    }
    free(samples);
    whisper_free(context);
    return status;
}

int main(int argc, char **argv)
{
    if (argc == 5 && strcmp(argv[1], "--prepare") == 0) {
        char *end;
        long threads = strtol(argv[4], &end, 10);
        const bool strict_gpu = strcmp(argv[3], "vulkan") == 0;
        const bool gpu = strict_gpu || strcmp(argv[3], "gpu") == 0;
        if (*end || threads < 1 || threads > 32 || (!gpu && strcmp(argv[3], "cpu") != 0))
            return 2;
        return prepare_stream(argv[2], gpu, (int)threads, strict_gpu);
    }
    if (argc == 4 && strcmp(argv[1], "--stream") == 0)
        return stream_audio(argv[2], argv[3], false);
    if (argc == 4 && strcmp(argv[1], "--live") == 0)
        return stream_audio(argv[2], argv[3], true);
    return aligned_file(argc, argv);
}
