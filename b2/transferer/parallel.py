######################################################################
#
# File: b2/transferer/parallel.py
#
# Copyright 2018 Backblaze Inc. All Rights Reserved.
#
# License https://www.backblaze.com/using_b2_code.html
#
######################################################################

from abc import abstractmethod
import logging
import hashlib
import threading

from six.moves import queue, range

from .abstract import AbstractDownloader

logger = logging.getLogger(__name__)


class ParallelDownloader(AbstractDownloader):
    # situations to consider:
    #
    # local file start                                         local file end
    # |                                                                     |
    # |                                                                     |
    # |      write range start                        write range end       |
    # |      |                                                      |       |
    # v      v                                                      v       v
    # #######################################################################
    #        |          |          |          |          |          |
    #         \        / \        / \        / \        / \        /
    #           part 1     part 2     part 3     part 4     part 5
    #         /        \ /        \ /        \ /        \ /        \
    #        |          |          |          |          |          |
    #      #######################################################################
    #      ^                                                                     ^
    #      |                                                                     |
    #      cloud file start                                         cloud file end
    #
    def __init__(self, chunk_size, max_streams, min_part_size):
        """
        :param chunk_size: internal buffer size
        :param max_streams: maximum number of simultaneous streams
        :param min_part_size: minimum amount of data a single stream will retrieve, in bytes
        """
        self.chunk_size = chunk_size
        self.max_streams = max_streams
        self.min_part_size = min_part_size
        super(ParallelDownloader, self).__init__()

    def is_suitable(self, metadata, progress_listener):
        return self._get_number_of_streams(
            metadata.content_length
        ) >= 2 and metadata.content_length >= 2 * self.min_part_size

    def _get_number_of_streams(self, content_length):
        return min(self.max_streams, content_length // self.min_part_size) or 1

    def download(
        self,
        file,
        response,
        metadata,
        session,
    ):
        """
        Downloads a file from given url using parallel download sessions and stores it in the given download_destination.

        :param file: an opened file-like object to write to
        :param response: The response of the first request made to the cloud service with download intent
        :return:
        """
        raw_request_range = response.request.headers.get('Range')  # 'bytes 0-11'
        if raw_request_range is None:
            range_ = (0, metadata.content_length)
            actual_size = metadata.content_length
        else:
            range_ = tuple(int(i) for i in raw_request_range.replace('bytes ', '').split('-'))
            actual_size = range_[1] - range_[0]

        start_file_position = file.tell()
        parts_to_download = gen_parts(
            Range(range_[0], range_[0] + actual_size),
            Range(start_file_position, start_file_position + actual_size),
            part_count=self._get_number_of_streams(metadata.content_length),
        )

        first_part = next(parts_to_download)

        hasher = hashlib.sha1()

        with WriterThread(file) as writer:
            self._get_parts(response, session, writer, hasher, first_part, parts_to_download)
        bytes_written = writer.total

        # At this point the hasher already consumed the data until the end of first stream.
        # Consume the rest of the file to complete the hashing process
        self._finish_hashing(first_part, file, hasher, metadata.content_length)

        return bytes_written, hasher.hexdigest()

    def _finish_hashing(self, first_part, file, hasher, content_length):
        end_of_first_part = first_part.local_range.end + 1
        file.seek(end_of_first_part)
        file_read = file.read

        last_offset = first_part.local_range.start + content_length
        current_offset = end_of_first_part
        stop = False
        while 1:
            data = file_read(self.chunk_size)
            if not data:
                break
            if current_offset + len(data) >= last_offset:
                to_hash = data[:last_offset - current_offset]
                stop = True
            else:
                to_hash = data
            hasher.update(data)
            current_offset += len(to_hash)
            if stop:
                break

    def _get_parts(self, response, session, writer, hasher, first_part, parts_to_download):
        stream = FirstPartDownloaderThread(
            response,
            hasher,
            session,
            writer,
            first_part,
            self.chunk_size,
        )
        stream.start()
        streams = [stream]

        for part in parts_to_download:
            stream = NonHashingDownloaderThread(
                response.request.url,
                session,
                writer,
                part,
                self.chunk_size,
            )
            stream.start()
            streams.append(stream)
        for stream in streams:
            stream.join()


class WriterThread(threading.Thread):
    def __init__(self, file):
        self.file = file
        self.queue = queue.Queue()
        self.total = 0
        super(WriterThread, self).__init__()

    def run(self):
        file = self.file
        queue_get = self.queue.get
        while 1:
            shutdown, offset, data = queue_get()
            if shutdown:
                break
            file.seek(offset)
            file.write(data)
            self.total += len(data)
            #print('writer total %i', self.total)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.queue.put((True, None, None))
        self.join()


class AbstractDownloaderThread(threading.Thread):
    def __init__(self, session, writer, part_to_download, chunk_size):
        """
        :param session: raw_api wrapper
        :param writer: where to write data
        :param part_to_download: PartToDownload object
        :param chunk_size: internal buffer size to use for writing and hashing
        """
        self.session = session
        self.writer = writer
        self.part_to_download = part_to_download
        self.chunk_size = chunk_size
        super(AbstractDownloaderThread, self).__init__()

    @abstractmethod
    def run(self):
        pass


class FirstPartDownloaderThread(AbstractDownloaderThread):
    def __init__(self, response, hasher, *args, **kwargs):
        """
        :param response: response of the original GET call
        :param hasher: hasher object to feed to as the stream is written
        """
        self.response = response
        self.hasher = hasher
        super(FirstPartDownloaderThread, self).__init__(*args, **kwargs)

    def run(self):
        writer_queue = self.writer.queue
        stop = False
        bytes_read = 0
        hasher_update = self.hasher.update
        first_offset = self.part_to_download.local_range.start
        last_offset = self.part_to_download.local_range.end + 1
        for data in self.response.iter_content(chunk_size=self.chunk_size):
            if first_offset + bytes_read + len(data) >= last_offset:
                to_write = data[:last_offset - bytes_read]
                stop = True
            else:
                to_write = data
            writer_queue.put((False, first_offset + bytes_read, to_write))
            hasher_update(to_write)
            bytes_read += len(to_write)
            if stop:
                break
        logging.debug('%s retrieved a total of %s bytes', self, bytes_read)
        # since we got everything we need, close the socket and free the buffer
        # to avoid a timeout exception during hashing and other trouble
        self.response.close()


class NonHashingDownloaderThread(AbstractDownloaderThread):
    def __init__(self, url, *args, **kwargs):
        """
        :param url: url of the target file
        """
        self.url = url
        super(NonHashingDownloaderThread, self).__init__(*args, **kwargs)

    def run(self):
        writer_queue_put = self.writer.queue.put
        start_range = self.part_to_download.local_range.start
        bytes_read = 0
        with self.session.download_file_from_url(
            self.url, self.part_to_download.cloud_range.as_tuple()
        ) as response:
            for to_write in response.iter_content(chunk_size=self.chunk_size):
                writer_queue_put((False, start_range + bytes_read, to_write))
                bytes_read += len(to_write)
        logging.debug('%s retrieved a total of %s bytes', self, bytes_read)


class Range(object):
    """
    HTTP ranges use an *inclusive* index at the end.
    """

    def __init__(self, start, end):
        assert 0 <= start <= end
        self.start = start
        self.end = end

    def size(self):
        return self.end - self.start + 1

    def subrange(self, sub_start, sub_end):
        """
        Returns a range that is part of this range.
        :param sub_start: Index relative to the start of this range.
        :param sub_end: (Inclusive!) index relative to the start of this range.
        :return: A new Range
        """
        assert 0 <= sub_start <= sub_end < self.size()
        return Range(self.start + sub_start, self.start + sub_end)

    def as_tuple(self):
        return self.start, self.end

    def __repr__(self):
        return 'Range(%d, %d)' % (self.start, self.end)


class PartToDownload(object):
    """
    Holds the range of a file to download, and the range of the
    local file where it should be stored.
    """

    def __init__(self, cloud_range, local_range):
        self.cloud_range = cloud_range
        self.local_range = local_range

    def __repr__(self):
        return 'PartToDownload(%s, %s)' % (self.cloud_range, self.local_range)


def gen_parts(cloud_range, local_range, part_count):
    """
    Generates a sequence of PartToDownload to download a large file as
    a collection of parts.
    """
    assert cloud_range.size() == local_range.size()
    assert 0 < part_count <= cloud_range.size()
    offset = 0
    remaining_size = cloud_range.size()
    for i in range(part_count):
        # This rounds down, so if the parts aren't all the same size,
        # the smaller parts will come first.
        this_part_size = remaining_size // (part_count - i)
        part = PartToDownload(
            cloud_range.subrange(offset, offset + this_part_size - 1),
            local_range.subrange(offset, offset + this_part_size - 1),
        )
        logger.debug('created part to download: %s', part)
        yield part
        offset += this_part_size
        remaining_size -= this_part_size
