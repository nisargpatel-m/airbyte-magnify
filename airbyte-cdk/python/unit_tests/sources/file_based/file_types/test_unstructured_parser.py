#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, mock_open, patch

import pytest
from airbyte_cdk.sources.file_based.config.unstructured_format import UnstructuredFormat
from airbyte_cdk.sources.file_based.exceptions import RecordParseError
from airbyte_cdk.sources.file_based.file_types import UnstructuredParser
from airbyte_cdk.sources.file_based.remote_file import RemoteFile
from unstructured.documents.elements import ElementMetadata, Formula, ListItem, Text, Title
from unstructured.file_utils.filetype import FileType

FILE_URI = "path/to/file.xyz"


@pytest.mark.parametrize(
    "filetype, format_config, raises",
    [
        pytest.param(
            FileType.MD,
            UnstructuredFormat(skip_unprocessable_files=False),
            False,
            id="markdown file",
        ),
        pytest.param(
            FileType.CSV,
            UnstructuredFormat(skip_unprocessable_files=False),
            True,
            id="wrong file format",
        ),
        pytest.param(
            FileType.CSV,
            UnstructuredFormat(skip_unprocessable_files=True),
            False,
            id="wrong file format skipping",
        ),
        pytest.param(
            FileType.PDF,
            UnstructuredFormat(skip_unprocessable_files=False),
            False,
            id="pdf file",
        ),
        pytest.param(
            FileType.DOCX,
            UnstructuredFormat(skip_unprocessable_files=False),
            False,
            id="docx file",
        ),
        pytest.param(
            FileType.PPTX,
            UnstructuredFormat(skip_unprocessable_files=False),
            False,
            id="pptx file",
        ),
    ],
)
@patch("airbyte_cdk.sources.file_based.file_types.unstructured_parser.detect_filetype")
def test_infer_schema(mock_detect_filetype, filetype, format_config, raises):
    # use a fresh event loop to avoid leaking into other tests
    main_loop = asyncio.get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stream_reader = MagicMock()
    mock_open(stream_reader.open_file)
    fake_file = MagicMock()
    fake_file.uri = FILE_URI
    logger = MagicMock()
    mock_detect_filetype.return_value = filetype
    config = MagicMock()
    config.format = format_config
    if raises:
        with pytest.raises(RecordParseError):
            loop.run_until_complete(UnstructuredParser().infer_schema(config, fake_file, stream_reader, logger))
    else:
        schema = loop.run_until_complete(UnstructuredParser().infer_schema(config, MagicMock(), MagicMock(), MagicMock()))
        assert schema == {
            "content": {"type": "string", "description": "Content of the file as markdown. Might be null if the file could not be parsed"},
            "document_key": {"type": "string", "description": "Unique identifier of the document, e.g. the file path"},
            "_ab_source_file_parse_error": {"type": "string", "description": "Error message if the file could not be parsed even though the file is supported"},
        }
    loop.close()
    asyncio.set_event_loop(main_loop)


@pytest.mark.parametrize(
    "filetype, format_config, parse_result, raises, expected_records, parsing_error",
    [
        pytest.param(
            FileType.MD,
            UnstructuredFormat(skip_unprocessable_files=False),
            "test",
            False,
            [
                {
                    "content": "test",
                    "document_key": FILE_URI,
                    "_ab_source_file_parse_error": None,
                }
            ],
            False,
            id="markdown file",
        ),
        pytest.param(
            FileType.CSV,
            UnstructuredFormat(skip_unprocessable_files=False),
            None,
            True,
            None,
            False,
            id="wrong file format",
        ),
        pytest.param(
            FileType.CSV,
            UnstructuredFormat(skip_unprocessable_files=True),
            None,
            False,
            [
                {
                    "content": None,
                    "document_key": FILE_URI,
                    "_ab_source_file_parse_error": "Error parsing record. This could be due to a mismatch between the config's file type and the actual file type, or because the file or record is not parseable. Contact Support if you need assistance.\nfilename=path/to/file.xyz message=File type FileType.CSV is not supported. Supported file types are FileType.MD, FileType.PDF, FileType.DOCX, FileType.PPTX",
                }
            ],
            False,
            id="skip_unprocessable_files",
        ),
        pytest.param(
            FileType.PDF,
            UnstructuredFormat(skip_unprocessable_files=False),
            [
                Title("heading"),
                Text("This is the text"),
                ListItem("This is a list item"),
                Formula("This is a formula"),
            ],
            False,
            [
                {
                    "content": "# heading\n\nThis is the text\n\n- This is a list item\n\n```\nThis is a formula\n```",
                    "document_key": FILE_URI,
                    "_ab_source_file_parse_error": None,
                }
            ],
            False,
            id="pdf file",
        ),
        pytest.param(
            FileType.PDF,
            UnstructuredFormat(skip_unprocessable_files=False),
            [
                Title("first level heading", metadata=ElementMetadata(category_depth=1)),
                Title("second level heading", metadata=ElementMetadata(category_depth=2)),
            ],
            False,
            [
                {
                    "content": "# first level heading\n\n## second level heading",
                    "document_key": FILE_URI,
                    "_ab_source_file_parse_error": None,
                }
            ],
            False,
            id="multi-level headings",
        ),
        pytest.param(
            FileType.DOCX,
            UnstructuredFormat(skip_unprocessable_files=False),
            [
                Title("heading"),
                Text("This is the text"),
                ListItem("This is a list item"),
                Formula("This is a formula"),
            ],
            False,
            [
                {
                    "content": "# heading\n\nThis is the text\n\n- This is a list item\n\n```\nThis is a formula\n```",
                    "document_key": FILE_URI,
                    "_ab_source_file_parse_error": None,
                }
            ],
            False,
            id="docx file",
        ),
        pytest.param(
            FileType.DOCX,
            UnstructuredFormat(skip_unprocessable_files=True),
            "",
            False,
            [
                {
                    "content": None,
                    "document_key": FILE_URI,
                    "_ab_source_file_parse_error": "Error parsing record. This could be due to a mismatch between the config's file type and the actual file type, or because the file or record is not parseable. Contact Support if you need assistance.\nfilename=path/to/file.xyz message=weird parsing error"
                }
            ],
            True,
            id="exception_during_parsing",
        ),
    ],
)
@patch("unstructured.partition.pdf.partition_pdf")
@patch("unstructured.partition.pptx.partition_pptx")
@patch("unstructured.partition.docx.partition_docx")
@patch("unstructured.partition.md.optional_decode")
@patch("airbyte_cdk.sources.file_based.file_types.unstructured_parser.detect_filetype")
def test_parse_records(
    mock_detect_filetype,
    mock_optional_decode,
    mock_partition_docx,
    mock_partition_pptx,
    mock_partition_pdf,
    filetype,
    format_config,
    parse_result,
    raises,
    expected_records,
    parsing_error,
):
    stream_reader = MagicMock()
    mock_open(stream_reader.open_file, read_data=bytes(str(parse_result), "utf-8"))
    fake_file = RemoteFile(uri=FILE_URI, last_modified=datetime.now())
    fake_file.uri = FILE_URI
    logger = MagicMock()
    config = MagicMock()
    config.format = format_config
    mock_detect_filetype.return_value = filetype
    if parsing_error:
        mock_partition_docx.side_effect = Exception("weird parsing error")
        mock_partition_pptx.side_effect = Exception("weird parsing error")
        mock_partition_pdf.side_effect = Exception("weird parsing error")
    else:
        mock_partition_docx.return_value = parse_result
        mock_partition_pptx.return_value = parse_result
        mock_partition_pdf.return_value = parse_result
    mock_optional_decode.side_effect = lambda x: x.decode("utf-8")
    if raises:
        with pytest.raises(RecordParseError):
            list(UnstructuredParser().parse_records(config, fake_file, stream_reader, logger, MagicMock()))
    else:
        assert list(UnstructuredParser().parse_records(config, fake_file, stream_reader, logger, MagicMock())) == expected_records
