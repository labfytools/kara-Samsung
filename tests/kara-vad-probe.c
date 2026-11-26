/* Measure Silero VAD on mono float32 PCM at WHISPER_SAMPLE_RATE. */
#define _POSIX_C_SOURCE 200809L
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include "whisper.h"

int main(int argc, char **argv) {
    if (argc != 3) {
        return 2;
    }
    int fd = open(argv[2], O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) {
        return 3;
    }
    struct stat input;
    if (fstat(fd, &input) < 0 || !S_ISREG(input.st_mode) || input.st_size <= 0
            || input.st_size > (off_t)sizeof(float) * WHISPER_SAMPLE_RATE * 3600
            || input.st_size % sizeof(float) != 0) {
        close(fd);
        return 3;
    }
    size_t bytes = (size_t)input.st_size;
    int samples = (int)(bytes / sizeof(float));
    const float *pcm = mmap(NULL, bytes, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (pcm == MAP_FAILED) {
        return 3;
    }
    struct whisper_vad_context_params config = whisper_vad_default_context_params();
    config.use_gpu = false;
    config.n_threads = 1;
    struct whisper_vad_context *vad = whisper_vad_init_from_file_with_params(argv[1], config);
    int status = 4;
    if (vad && whisper_vad_detect_speech(vad, pcm, samples)) {
        const float thresholds[] = {0.35f, 0.5f, 0.65f};
        status = 0;
        for (size_t i = 0; i < sizeof(thresholds) / sizeof(thresholds[0]); ++i) {
            /* The installed API returns centiseconds. Reuse the same
             * emission probabilities for all threshold decisions. */
            struct whisper_vad_params options = whisper_vad_default_params();
            options.threshold = thresholds[i];
            struct whisper_vad_segments *segments = whisper_vad_segments_from_probs(vad, options);
            if (!segments) {
                status = 5;
                break;
            }
            int length = whisper_vad_segments_n_segments(segments);
            for (int j = 0; j < length; ++j) {
                printf("%.2f %.4f %.4f\n", (double)thresholds[i],
                       whisper_vad_segments_get_segment_t0(segments, j) / 100.,
                       whisper_vad_segments_get_segment_t1(segments, j) / 100.);
            }
            whisper_vad_free_segments(segments);
        }
    }
    if (vad) {
        whisper_vad_free(vad);
    }
    munmap((void *)pcm, bytes);
    return status;
}
