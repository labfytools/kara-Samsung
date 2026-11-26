// Read-only short-lived helper for the Parole inventory. The index still
// owns file-version checks, metadata precedence and FFprobe fallback.
#include <taglib/audioproperties.h>
#include <taglib/fileref.h>
#include <taglib/flacfile.h>
#include <taglib/mp4file.h>
#include <taglib/mpegfile.h>
#include <taglib/opusfile.h>
#include <taglib/tpropertymap.h>
#include <taglib/vorbisfile.h>

#include <iostream>
#include <string>
#include <string_view>
#include <sys/resource.h>

namespace {

constexpr std::size_t max_output = 1024 * 1024;

std::string_view codec(TagLib::File *file)
{
    if (dynamic_cast<TagLib::FLAC::File *>(file))
        return "flac";
    if (dynamic_cast<TagLib::Ogg::Opus::File *>(file))
        return "opus";
    if (dynamic_cast<TagLib::Ogg::Vorbis::File *>(file))
        return "vorbis";
    if (const auto *mpeg = dynamic_cast<TagLib::MPEG::File *>(file)) {
        const auto *properties = mpeg->audioProperties();
        if (properties && properties->layer() == 3)
            return "mp3";
    }
    if (const auto *mp4 = dynamic_cast<TagLib::MP4::File *>(file)) {
        const auto *properties = mp4->audioProperties();
        if (properties && properties->codec() == TagLib::MP4::Properties::AAC)
            return "aac";
        if (properties && properties->codec() == TagLib::MP4::Properties::ALAC)
            return "alac";
    }
    return {};
}

bool field(std::string &output, std::string_view value)
{
    // NUL-delimited UTF-8 fields preserve spaces and newlines in lyrics.
    // Reject representations that cannot fit the protocol rather than truncating.
    if (value.find('\0') != std::string_view::npos || value.size() >= max_output - output.size())
        return false;
    output.append(value);
    output.push_back('\0');
    return true;
}

} // namespace

int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;
    // TagLib owns native parsing and any allocation for embedded artwork.
    // Each child has a bounded address space and never transfers image bytes.
    const rlimit memory{128 * 1024 * 1024, 128 * 1024 * 1024};
    if (setrlimit(RLIMIT_AS, &memory) != 0)
        return 2;
    try {
        TagLib::FileRef file(argv[1], true, TagLib::AudioProperties::Average);
        if (file.isNull())
            return 2;
        const auto format = codec(file.file());
        const auto *properties = file.audioProperties();
        const int milliseconds = properties ? properties->lengthInMilliseconds() : 0;
        if (format.empty() || milliseconds <= 0)
            return 2;

        const auto tags = file.properties();
        const auto complex = file.complexPropertyKeys();
        const bool artwork = complex.contains("PICTURE");
        for (const auto &key : complex) {
            if (key != "PICTURE")
                return 2;
        }
        for (const auto &key : tags.unsupportedData()) {
            if (!artwork || (key != "APIC" && key != "covr"))
                return 2;
        }

        std::string output;
        if (!field(output, "parole-tags-v1") || !field(output, std::to_string(milliseconds)) ||
            !field(output, format) || !field(output, artwork ? "1" : "0"))
            return 2;
        for (const auto &[key, values] : tags) {
            // FFprobe and TagLib can flatten multivalued credits differently.
            if (values.size() != 1 || !field(output, key.to8Bit(true)) ||
                !field(output, values.front().to8Bit(true)))
                return 2;
        }
        std::cout.write(output.data(), static_cast<std::streamsize>(output.size()));
        return std::cout ? 0 : 2;
    } catch (...) {
        return 2;
    }
}
