#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import logging
from io import BytesIO, IOBase
from typing import Any, Dict, Iterable, List, Mapping, Optional

from airbyte_cdk.sources.file_based.config.file_based_stream_config import FileBasedStreamConfig
from airbyte_cdk.sources.file_based.config.unstructured_format import UnstructuredFormat
from airbyte_cdk.sources.file_based.exceptions import FileBasedSourceError, RecordParseError
from airbyte_cdk.sources.file_based.file_based_stream_reader import AbstractFileBasedStreamReader, FileReadMode
from airbyte_cdk.sources.file_based.file_types.file_type_parser import FileTypeParser
from airbyte_cdk.sources.file_based.remote_file import RemoteFile
from airbyte_cdk.sources.file_based.schema_helpers import SchemaType
from unstructured.documents.elements import Formula, ListItem, Title
from unstructured.file_utils.filetype import STR_TO_FILETYPE, FileType, detect_filetype

unstructured_partition_pdf = None
unstructured_partition_docx = None
unstructured_partition_pptx = None
unstructured_optional_decode = None


def _import_unstructured() -> None:
    """Dynamically imported as needed, due to slow import speed."""
    global unstructured_partition_pdf
    global unstructured_partition_docx
    global unstructured_partition_pptx
    global unstructured_optional_decode
    from unstructured.partition.docx import partition_docx
    from unstructured.partition.md import optional_decode
    from unstructured.partition.pdf import partition_pdf
    from unstructured.partition.pptx import partition_pptx

    # separate global variables to properly propagate typing
    unstructured_partition_pdf = partition_pdf
    unstructured_partition_docx = partition_docx
    unstructured_partition_pptx = partition_pptx
    unstructured_optional_decode = optional_decode


class UnstructuredParser(FileTypeParser):
    @property
    def parser_max_n_files_for_schema_inference(self) -> Optional[int]:
        """
        Just check one file as the schema is static
        """
        return 1

    @property
    def parser_max_n_files_for_parsability(self) -> Optional[int]:
        """
        Do not check any files for parsability because it might be an expensive operation and doesn't give much confidence whether the sync will succeed.
        """
        return 0

    async def infer_schema(
        self,
        config: FileBasedStreamConfig,
        file: RemoteFile,
        stream_reader: AbstractFileBasedStreamReader,
        logger: logging.Logger,
    ) -> SchemaType:
        format = _extract_format(config)
        with stream_reader.open_file(file, self.file_read_mode, None, logger) as file_handle:
            filetype = self._get_filetype(file_handle, file)

            if filetype not in self._supported_file_types() and not format.skip_unprocessable_files:
                raise self._create_parse_error(file, self._get_file_type_error_message(filetype))

            return {
                "content": {
                    "type": "string",
                    "description": "Content of the file as markdown. Might be null if the file could not be parsed",
                },
                "document_key": {"type": "string", "description": "Unique identifier of the document, e.g. the file path"},
                "_ab_source_file_parse_error": {
                    "type": "string",
                    "description": "Error message if the file could not be parsed even though the file is supported",
                },
            }

    def parse_records(
        self,
        config: FileBasedStreamConfig,
        file: RemoteFile,
        stream_reader: AbstractFileBasedStreamReader,
        logger: logging.Logger,
        discovered_schema: Optional[Mapping[str, SchemaType]],
    ) -> Iterable[Dict[str, Any]]:
        format = _extract_format(config)
        with stream_reader.open_file(file, self.file_read_mode, None, logger) as file_handle:
            try:
                markdown = self._read_file(file_handle, file, format, logger)
                yield {
                    "content": markdown,
                    "document_key": file.uri,
                    "_ab_source_file_parse_error": None,
                }
            except RecordParseError as e:
                # RecordParseError is raised when the file can't be parsed because of a problem with the file content (either the file is not supported or the file is corrupted)
                # if the skip_unprocessable_files flag is set, we log a warning and pass the error as part of the document
                # otherwise, we raise the error to fail the sync
                if format.skip_unprocessable_files:
                    exception_str = str(e)
                    logger.warn(f"File {file.uri} caused an error during parsing: {exception_str}.")
                    yield {
                        "content": None,
                        "document_key": file.uri,
                        "_ab_source_file_parse_error": exception_str,
                    }
                    logger.warn(f"File {file.uri} cannot be parsed. Skipping it.")
                else:
                    raise e

    def _read_file(self, file_handle: IOBase, remote_file: RemoteFile, format: UnstructuredFormat, logger: logging.Logger) -> str:
        _import_unstructured()
        if (
            (not unstructured_partition_pdf)
            or (not unstructured_partition_docx)
            or (not unstructured_partition_pptx)
            or (not unstructured_optional_decode)
        ):
            # check whether unstructured library is actually available for better error message and to ensure proper typing (can't be None after this point)
            raise Exception("unstructured library is not available")

        filetype = self._get_filetype(file_handle, remote_file)

        if filetype == FileType.MD:
            file_content: bytes = file_handle.read()
            decoded_content: str = unstructured_optional_decode(file_content)
            return decoded_content
        if filetype not in self._supported_file_types():
            raise self._create_parse_error(remote_file, self._get_file_type_error_message(filetype))

        file: Any = file_handle

        # before the parsing logic is entered, the file is read completely to make sure it is in local memory
        file_handle.seek(0)
        file_handle.read()
        file_handle.seek(0)

        try:
            if filetype == FileType.PDF:
                # for PDF, read the file into a BytesIO object because some code paths in pdf parsing are doing an instance check on the file object and don't work with file-like objects
                with BytesIO(file_handle.read()) as file:
                    file_handle.seek(0)
                    elements = unstructured_partition_pdf(file=file)
            elif filetype == FileType.DOCX:
                elements = unstructured_partition_docx(file=file)
            elif filetype == FileType.PPTX:
                elements = unstructured_partition_pptx(file=file)
        except Exception as e:
            raise self._create_parse_error(remote_file, str(e))

        return self._render_markdown(elements)

    def _create_parse_error(self, remote_file: RemoteFile, message: str) -> RecordParseError:
        return RecordParseError(FileBasedSourceError.ERROR_PARSING_RECORD, filename=remote_file.uri, message=message)

    def _get_filetype(self, file: IOBase, remote_file: RemoteFile) -> Optional[FileType]:
        """
        Detect the file type based on the file name and the file content.

        There are three strategies to determine the file type:
        1. Use the mime type if available (only some sources support it)
        2. Use the file name if available
        3. Use the file content
        """
        if remote_file.mime_type and remote_file.mime_type in STR_TO_FILETYPE:
            return STR_TO_FILETYPE[remote_file.mime_type]

        # set name to none, otherwise unstructured will try to get the modified date from the local file system
        if hasattr(file, "name"):
            file.name = None

        # detect_filetype is either using the file name or file content
        # if possible, try to leverage the file name to detect the file type
        # if the file name is not available, use the file content
        file_type = detect_filetype(
            filename=remote_file.uri,
        )
        if file_type is not None and not file_type == FileType.UNK:
            return file_type

        type_based_on_content = detect_filetype(file=file)

        # detect_filetype is reading to read the file content
        file.seek(0)

        return type_based_on_content

    def _supported_file_types(self) -> List[Any]:
        return [FileType.MD, FileType.PDF, FileType.DOCX, FileType.PPTX]

    def _get_file_type_error_message(self, file_type: FileType) -> str:
        supported_file_types = ", ".join([str(type) for type in self._supported_file_types()])
        return f"File type {file_type} is not supported. Supported file types are {supported_file_types}"

    def _render_markdown(self, elements: List[Any]) -> str:
        return "\n\n".join((self._convert_to_markdown(el) for el in elements))

    def _convert_to_markdown(self, el: Any) -> str:
        if isinstance(el, Title):
            heading_str = "#" * (el.metadata.category_depth or 1)
            return f"{heading_str} {el.text}"
        elif isinstance(el, ListItem):
            return f"- {el.text}"
        elif isinstance(el, Formula):
            return f"```\n{el.text}\n```"
        else:
            return str(el.text) if hasattr(el, "text") else ""

    @property
    def file_read_mode(self) -> FileReadMode:
        return FileReadMode.READ_BINARY


def _extract_format(config: FileBasedStreamConfig) -> UnstructuredFormat:
    config_format = config.format
    if not isinstance(config_format, UnstructuredFormat):
        raise ValueError(f"Invalid format config: {config_format}")
    return config_format
