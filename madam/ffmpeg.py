import io
import json
import re
import shutil
import subprocess
import tempfile
from collections import MutableMapping

from bidict import bidict

from madam.core import Asset, MetadataProcessor, Processor, operator, OperatorError, UnsupportedFormatError
from madam.future import CalledProcessError, subprocess_run


class FFmpegProcessor(Processor):
    """
    Represents a processor that uses FFmpeg to read audio and video data.

    The minimum version of FFmpeg required is v0.9.
    """

    @staticmethod
    def __probe(file, format=True, streams=False):
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(file.read())
            tmp.flush()

            command = 'ffprobe -print_format json -loglevel quiet'.split()
            if format:
                command.append('-show_format')
            if streams:
                command.append('-show_streams')
            command.append(tmp.name)
            result = subprocess_run(command, stdout=subprocess.PIPE)
        string_result = result.stdout.decode('utf-8')
        json_obj = json.loads(string_result)
        return json_obj

    def __init__(self):
        """
        Initializes a new FFmpegProcessor.

        :raises EnvironmentError: if the installed version of ffprobe does not match the minimum version requirement
        """
        super().__init__()

        self._min_version = '0.9'
        command = 'ffprobe -version'.split()
        result = subprocess_run(command, stdout=subprocess.PIPE)
        string_result = result.stdout.decode('utf-8')
        version_string = string_result.split()[2]
        if version_string < self._min_version:
            raise EnvironmentError('Found ffprobe version %s. Requiring at least version %s.'
                                   % (version_string, self._min_version))

        self.__mime_type_to_ffmpeg_type = bidict({
            'audio/mpeg': 'mp3',
            'audio/ogg': 'ogg',
            'audio/wav': 'wav',
            'video/mp4': 'mp4',
            'video/webm': 'webm',
            'video/x-yuv4mpegpipe': 'yuv4mpegpipe'
        })

    def _can_read(self, file):
        file_info = FFmpegProcessor.__probe(file)
        return bool(file_info)

    def _read(self, file):
        file_info = FFmpegProcessor.__probe(file, streams=True)
        file.seek(0)

        for format_name in file_info['format']['format_name'].split(','):
            mime_type = self.__mime_type_to_ffmpeg_type.inv.get(format_name)
            if mime_type is not None:
                break
        metadata = dict(
            mime_type=mime_type,
            duration=float(file_info['format']['duration'])
        )
        for stream in file_info['streams']:
            stream_type = stream.get('codec_type')
            if stream_type in ('audio', 'video'):
                # Only use first stream
                if stream_type in metadata:
                    break
                metadata[stream_type] = {}
            if 'codec_name' in stream:
                metadata[stream_type]['codec'] = stream['codec_name']
            if 'bit_rate' in stream:
                metadata[stream_type]['bitrate'] = float(stream['bit_rate'])/1000.0

        return Asset(essence=file, **metadata)

    @operator
    def resize(self, asset, width, height):
        if width < 1 or height < 1:
            raise ValueError('Invalid dimensions: %dx%d' % (width, height))

        try:
            ffmpeg_type = self.__mime_type_to_ffmpeg_type[asset.mime_type]
        except KeyError:
            raise UnsupportedFormatError('Unsupported asset type: %s' % asset.mime_type)
        if asset.mime_type.split('/')[0] not in ('image', 'video'):
            raise OperatorError('Cannot resize asset of type %s')

        with tempfile.NamedTemporaryFile() as tmp:
            command = ['ffmpeg', '-loglevel', 'error', '-f', ffmpeg_type, '-i', 'pipe:',
                       '-filter:v', 'scale=%d:%d' % (width, height),
                       '-f', ffmpeg_type, '-y', tmp.name]

            try:
                subprocess_run(command, input=asset.essence.read(),
                               stderr=subprocess.PIPE, check=True)
            except CalledProcessError as ffmpeg_error:
                error_message = ffmpeg_error.stderr.decode('utf-8')
                raise OperatorError('Could not resize video asset: %s' % error_message)

            return Asset(essence=tmp, width=width, height=height)

    @operator
    def convert(self, asset, mime_type, video=None, audio=None, subtitles=None):
        """
        Creates a new asset of the specified MIME type from the essence of the
        specified asset.

        Additional options can be specified for video, audio, and subtitle streams.
        Options are passed as dictionary instances and can contain various keys for
        each stream type.

        **Options for video streams:**

        - **codec** – Processor-specific name of the video codec as string
        - **bitrate** – Target bitrate in kBit/s as float number

        **Options for audio streams:**

        - **codec** – Processor-specific name of the audio codec as string
        - **bitrate** – Target bitrate in kBit/s as float number

        **Options for subtitle streams:**

        - **codec** – Processor-specific name of the subtitle format as string

        :param asset: Asset whose contents will be converted
        :param mime_type: MIME type of the video container
        :param video: Dictionary with options for video streams.
        :param audio: Dictionary with options for audio streams.
        :param subtitles: Dictionary with the options for subtitle streams.
        :return: New asset with converted essence
        """
        ffmpeg_type = self.__mime_type_to_ffmpeg_type[mime_type]

        command = ['ffmpeg', '-loglevel', 'error', '-i', 'pipe:']
        if video is not None:
            if 'codec' in video: command.extend(['-c:v', video['codec']])
            if 'bitrate' in video: command.extend(['-b:v', '%dk' % video['bitrate']])
        if audio is not None:
            if 'codec' in audio: command.extend(['-c:a', audio['codec']])
            if 'bitrate' in audio: command.extend(['-b:a', '%dk' % audio['bitrate']])
        if subtitles is not None:
            if 'codec' in subtitles: command.extend(['-c:s', subtitles['codec']])
        with tempfile.NamedTemporaryFile() as tmp:
            command.extend(['-f', ffmpeg_type, '-y', tmp.name])

            try:
                subprocess_run(command, input=asset.essence.read(),
                               stderr=subprocess.PIPE, check=True)
            except CalledProcessError as ffmpeg_error:
                error_message = ffmpeg_error.stderr.decode('utf-8')
                raise OperatorError('Could not convert video asset: %s' % error_message)

            return Asset(essence=tmp, mime_type=mime_type)


class FFMetadataParser(MutableMapping):
    """Parses FFmpeg's metadata ini-like file format.

    See https://www.ffmpeg.org/ffmpeg-formats.html#Metadata-1 for a specification.
    """
    SUPPORTED_VERSIONS = 1,
    GLOBAL_SECTION = '__global__'
    HEADER_PATTERN = re.compile(r'^;FFMETADATA(?P<version>\d+)$')
    COMMENT_PATTERN = re.compile(r'^[;#]')
    SECTION_PATTERN = re.compile(r'\[(?P<section_name>[A-Z]+)\]')
    KEY_VALUE_PATTERN = re.compile(r'^(?P<key>.+)(?<!\\)=(?P<value>.*)$')
    ESCAPE_PATTERN = re.compile(r'\\(?P<special_character>.)')
    MISSING_ESCAPE_PATTERN = re.compile(r'(?<!\\)(?P<unescaped_character>[=;#])')

    def __init__(self):
        self.__metadata = {}

    @staticmethod
    def __replace_escaping(m):
        repl_char = m.group('special_character')
        if repl_char not in r'=;#\\':
            raise ValueError('Invalid syntax: Character %r cannot be escaped' % repl_char)
        return repl_char

    @staticmethod
    def __unescape(string):
        missing_escaping_match = FFMetadataParser.MISSING_ESCAPE_PATTERN.search(string)
        if missing_escaping_match:
            unescaped_char = missing_escaping_match.group('unescaped_character')
            raise ValueError('Invalid syntax: Character %r must be escaped' % unescaped_char)
        return FFMetadataParser.ESCAPE_PATTERN.sub(FFMetadataParser.__replace_escaping, string)

    def _read(self, lines):
        first_line = next(lines)
        header_match = FFMetadataParser.HEADER_PATTERN.match(first_line)
        if not header_match:
            raise ValueError('Unrecognized file format: Unknown header for FFmpeg metadata: %r' % first_line)
        version = int(header_match.group('version'))
        if not header_match or version not in FFMetadataParser.SUPPORTED_VERSIONS:
            raise ValueError('Unknown file format version: %d' % version)
        self.__metadata.clear()
        section_name = FFMetadataParser.GLOBAL_SECTION
        section = {}
        line_no = 2
        was_escaped_newline = False
        previous_line = ''
        for line in lines:
            if was_escaped_newline:
                line = previous_line + line
                was_escaped_newline = False
            if line.endswith('\\'):
                was_escaped_newline = True
                previous_line = line[:-1]
                continue
            if not line or FFMetadataParser.COMMENT_PATTERN.match(line):
                continue
            section_match = FFMetadataParser.SECTION_PATTERN.match(line)
            if section_match:
                self.__metadata[section_name] = section
                section_name = section_match.group('section_name')
                section = {}
                continue
            key_value_match = FFMetadataParser.KEY_VALUE_PATTERN.match(line)
            if key_value_match:
                key = FFMetadataParser.__unescape(key_value_match.group('key'))
                value = FFMetadataParser.__unescape(key_value_match.group('value'))
                section[key] = value
            else:
                raise ValueError('Invalid syntax in line %d: %r' % (line_no, line))
        self.__metadata[section_name] = section

    def read_string(self, string):
        lines = string.splitlines()
        return self._read(iter(lines))

    def __getitem__(self, key):
        return self.__metadata[key]

    def __setitem__(self, key, value):
        self.__metadata[key] = value

    def __delitem__(self, key):
        del self.__metadata[key]

    def __iter__(self):
        return iter(self.__metadata)

    def __len__(self):
        return len(self.__metadata)


class FFmpegMetadataProcessor(MetadataProcessor):
    """
    Represents a metadata processor that uses FFmpeg.
    """
    __DECODER_TO_ENCODER = {
        'matroska,webm': 'matroska',
        'mov,mp4,m4a,3gp,3g2,mj2': 'mov',
        'mp3': 'mp3',
        'ogg': 'ogg'
    }

    @property
    def formats(self):
        return 'ffmetadata',

    def read(self, file):
        command = 'ffmpeg -loglevel error -i pipe: -codec copy -y -f ffmetadata pipe:'.split()
        try:
            result = subprocess_run(command, input=file.read(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=True)
        except CalledProcessError as ffmpeg_error:
            error_message = ffmpeg_error.stderr.decode('utf-8')
            if 'Invalid data found when processing input' in error_message:
                raise UnsupportedFormatError('Unknown file format.')
            else:
                raise OperatorError('Could not read metadata from asset: %s' % error_message)

        data = result.stdout.decode('utf-8')
        parser = FFMetadataParser()
        parser.read_string(data)

        ffmetadata = parser[FFMetadataParser.GLOBAL_SECTION]
        # FFmpeg always returns the encoder, even when there was no "real" metadata
        if not ffmetadata or (len(ffmetadata) == 1 and 'encoder' in ffmetadata):
            return {}

        return {'ffmetadata': parser[FFMetadataParser.GLOBAL_SECTION]}

    def __get_encoder(self, file):
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(file.read())
            tmp.flush()

            command = 'ffprobe -loglevel error -print_format json -show_format'.split()
            command.append(tmp.name)
            try:
                result = subprocess_run(command, input=file.read(), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, check=True)
            except CalledProcessError:
                raise UnsupportedFormatError('Unknown file format.')
            probe_data = json.loads(result.stdout.decode('utf-8'))
            decoder_name = probe_data['format']['format_name']

        file.seek(0)

        return FFmpegMetadataProcessor.__DECODER_TO_ENCODER.get(decoder_name)

    def strip(self, file):
        # Determine encoder for output
        encoder_name = self.__get_encoder(file)
        if encoder_name is None:
            return file

        # Strip metadata
        result = io.BytesIO()
        with tempfile.NamedTemporaryFile() as tmp:
            command = 'ffmpeg -loglevel error -i pipe: -map_metadata -1 -codec copy -y -f'.split()
            command.append(encoder_name)
            command.append(tmp.name)
            try:
                subprocess_run(command, input=file.read(),
                               stderr=subprocess.PIPE, check=True)
            except CalledProcessError as ffmpeg_error:
                error_message = ffmpeg_error.stderr.decode('utf-8')
                raise OperatorError('Could not strip metadata: %s' % error_message)

            shutil.copyfileobj(tmp, result)
            result.seek(0)

        return result

    def combine(self, file, metadata_by_type):
        if not metadata_by_type:
            raise ValueError('No metadata provided')
        if 'ffmetadata' not in metadata_by_type:
            raise UnsupportedFormatError('Invalid metadata to be combined with essence: %r' %
                                         (metadata_by_type.keys(),))

        # Determine encoder for output
        encoder_name = self.__get_encoder(file)
        if encoder_name is None:
            return file

        # Add metadata to file
        result = io.BytesIO()
        with tempfile.NamedTemporaryFile() as tmp:
            command = ['ffmpeg', '-loglevel', 'error', '-i', 'pipe:']

            for item in metadata_by_type['ffmetadata'].items():
                command.append('-metadata')
                command.append('%s=%s' % item)

            command.extend(['-codec', 'copy', '-y', '-f', encoder_name, tmp.name])
            try:
                subprocess_run(command, input=file.read(),
                               stderr=subprocess.PIPE, check=True)
            except CalledProcessError as ffmpeg_error:
                error_message = ffmpeg_error.stderr.decode('utf-8')
                raise OperatorError('Could not add metadata: %s' % error_message)

            shutil.copyfileobj(tmp, result)
            result.seek(0)

        return result
