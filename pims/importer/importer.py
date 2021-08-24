# * Copyright (c) 2020. Authors: see NOTICE file.
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# *      http://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
import logging
import shutil
from datetime import datetime

from pims.api.exceptions import FilepathNotFoundProblem, NoMatchingFormatProblem, MetadataParsingProblem, \
    BadRequestException
from pims.api.utils.models import HistogramType
from pims.config import get_settings
from pims.files.archive import Archive, ArchiveError
from pims.files.file import Path, HISTOGRAM_STEM, UPLOAD_DIR_PREFIX, PROCESSED_DIR, ORIGINAL_STEM, EXTRACTED_DIR, \
    SPATIAL_STEM
from pims.files.histogram import build_histogram_file
from pims.files.image import Image
from pims.formats.utils.factories import FormatFactory, SpatialReadableFormatFactory
from pims.importer.listeners import ImportEventType

log = logging.getLogger("pims.app")

PENDING_PATH = Path(get_settings().pending_path)
FILE_ROOT_PATH = Path(get_settings().root)


def unique_name_generator():
    return int(datetime.now().timestamp() * 1e6)


class FileErrorProblem(BadRequestException):
    pass


class ImageParsingProblem(BadRequestException):
    pass


class FormatConversionProblem(BadRequestException):
    pass


class FileImporter:
    """
    Image importer from file. It moves a pending file to PIMS root path, tries to
    identify the file format, converts it if needed and checks its integrity.

    Attributes
    ----------
    pending_file : Path
        A file to import from PENDING_PATH directory
    pending_name : str (optional)
        A name to use for the pending file.
        If not provided, the current pending file name is used.
    loggers : list of ImportLogger (optional)
        A list of import loggers

    """
    def __init__(self, pending_file, pending_name=None, loggers=None):
        self.loggers = loggers if loggers is not None else []
        self.pending_file = pending_file
        self.pending_name = pending_name

        self.upload_dir = None
        self.upload_path = None
        self.original_path = None
        self.original = None
        self.spatial_path = None
        self.spatial = None
        self.histogram_path = None
        self.histogram = None

        self.processed_dir = None
        self.extracted_dir = None

    def notify(self, method, *args, **kwargs):
        for logger in self.loggers:
            try:
                getattr(logger, method)(*args, **kwargs)
            except AttributeError:
                log.warning(f"No method {method} for import logger {logger}")

    def run(self, prefer_copy=False):
        """
        Import the pending file. It moves a pending file to PIMS root path, tries to
        identify the file format, converts it if needed and checks its integrity.

        Parameters
        ----------
        prefer_copy : bool
            Prefer copy the pending file instead of moving it. Useful for tests.

        Returns
        -------
        images : list of Image
            A list of images imported from the pending file.

        Raises
        ------
        FilepathNotFoundProblem
            If pending file is not found.
        """
        try:
            self.notify(ImportEventType.START_DATA_EXTRACTION, self.pending_file)

            # Check the file is in pending area.
            if self.pending_file.parent != PENDING_PATH or \
                    not self.pending_file.exists():
                self.notify(ImportEventType.FILE_NOT_FOUND, self.pending_file)
                raise FilepathNotFoundProblem(self.pending_file)

            # Move the file to PIMS root path
            upload_dir_name = Path(f"{UPLOAD_DIR_PREFIX}"
                                   f"{str(unique_name_generator())}")
            self.upload_dir = FILE_ROOT_PATH / upload_dir_name
            self.mkdir(self.upload_dir)

            if self.pending_name:
                name = self.pending_name
            else:
                name = self.pending_file.name
            self.upload_path = self.upload_dir / name

            self.move(self.pending_file, self.upload_path, prefer_copy)
            self.notify(ImportEventType.MOVED_PENDING_FILE,
                        self.pending_file, self.upload_path)
            self.notify(ImportEventType.END_DATA_EXTRACTION, self.upload_path)

            # Identify format
            self.notify(ImportEventType.START_FORMAT_DETECTION, self.upload_path)

            format_factory = FormatFactory()
            format = format_factory.match(self.upload_path)
            archive = None
            if format is None:
                archive = Archive.from_path(self.upload_path)
                if archive:
                    format = archive.format

            if format is None:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.upload_path)
                raise NoMatchingFormatProblem(self.upload_path)
            self.notify(ImportEventType.END_FORMAT_DETECTION,
                        self.upload_path, format)

            # Create processed dir
            self.processed_dir = self.upload_dir / Path(PROCESSED_DIR)
            self.mkdir(self.processed_dir)

            # Create original role
            original_filename = Path(
                f"{ORIGINAL_STEM}.{format.get_identifier()}"
            )
            self.original_path = self.processed_dir / original_filename
            if archive:
                try:
                    self.notify(
                        ImportEventType.START_UNPACKING, self.upload_path
                    )
                    archive.extract(self.original_path)
                except ArchiveError as e:
                    self.notify(
                        ImportEventType.ERROR_UNPACKING, self.upload_path,
                        exception=e
                    )
                    raise FileErrorProblem(self.upload_path)
                
                # Now the archive is extracted, check if it's a multi-file format
                format = format_factory.match(self.original_path)
                if format:
                    # It is a multi-file format
                    original_filename = Path(
                        f"{ORIGINAL_STEM}.{format.get_identifier()}"
                    )
                    new_original_path = self.processed_dir / original_filename
                    self.move(self.original_path, new_original_path)
                    self.original_path = new_original_path

                    self.notify(
                        ImportEventType.END_UNPACKING, self.upload_path,
                        self.original_path, format=format, is_collection=False
                    )
                else:
                    # TODO: add bg tasks for every file
                    self.notify(
                        ImportEventType.END_UNPACKING, self.upload_path,
                        self.original_path, is_collection=True
                    )
                    raise NotImplementedError
            else:
                self.mksymlink(self.original_path, self.upload_path)
                assert self.original_path.has_original_role()

            # Check original image integrity
            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.original_path)
            self.original = Image(self.original_path, format=format)
            errors = self.original.check_integrity(metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK, self.original_path,
                    integrity_errors=errors
                )
                raise ImageParsingProblem(self.original)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.original)

            if format.is_spatial():
                self.deploy_spatial(format)
            else:
                raise NotImplementedError()

            self.deploy_histogram(self.original.get_spatial())

            # Finished
            self.notify(ImportEventType.END_SUCCESSFUL_IMPORT,
                        self.upload_path, self.original)
            return [self.upload_path]
        except Exception as e:
            self.notify(ImportEventType.FILE_ERROR,
                        self.upload_path, exeception=e)
            raise e

    def deploy_spatial(self, format):
        self.notify(ImportEventType.START_SPATIAL_DEPLOY, self.original_path)
        if format.need_conversion:
            # Do the spatial conversion
            try:
                ext = format.conversion_format().get_identifier()
                spatial_filename = Path(f"{SPATIAL_STEM}.{ext}")
                self.spatial_path = self.processed_dir / spatial_filename
                self.notify(ImportEventType.START_CONVERSION,
                            self.spatial_path, self.upload_path)

                r = format.convert(self.spatial_path)
                if not r or not self.spatial_path.exists():
                    self.notify(ImportEventType.ERROR_CONVERSION,
                                self.spatial_path)
                    raise FormatConversionProblem()
            except Exception as e:
                self.notify(ImportEventType.ERROR_CONVERSION,
                            self.spatial_path, exception=e)
                raise FormatConversionProblem()
            
            self.notify(ImportEventType.END_CONVERSION, self.spatial_path)

            # Check format of converted file
            self.notify(ImportEventType.START_FORMAT_DETECTION, self.spatial_path)
            spatial_format = SpatialReadableFormatFactory().match(self.spatial_path)
            if not spatial_format:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.spatial_path)
                raise NoMatchingFormatProblem(self.spatial_path)
            self.notify(ImportEventType.END_FORMAT_DETECTION, 
                        self.spatial_path, spatial_format)

            self.spatial = Image(self.spatial_path, format=spatial_format)

            # Check spatial image integrity
            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.spatial_path)
            errors = self.spatial.check_integrity(metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK, self.spatial_path,
                    integrity_errors=errors
                )
                raise ImageParsingProblem(self.spatial)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.spatial)
            
        else:
            # Create spatial role
            spatial_filename = Path(f"{SPATIAL_STEM}.{format.get_identifier()}")
            self.spatial_path = self.processed_dir / spatial_filename
            self.mksymlink(self.spatial_path, self.original_path)
            self.spatial = Image(self.spatial_path, format=format)

        assert self.spatial.has_spatial_role()
        self.notify(ImportEventType.END_SPATIAL_DEPLOY, self.spatial)
        return self.spatial

    def deploy_histogram(self, image):
        self.histogram_path = self.processed_dir / Path(HISTOGRAM_STEM)
        self.notify(ImportEventType.START_HISTOGRAM_DEPLOY,
                    self.histogram_path, image)
        try:
            self.histogram = build_histogram_file(
                image, self.histogram_path, HistogramType.FAST
            )
        except (FileNotFoundError, FileExistsError) as e:
            self.notify(
                ImportEventType.ERROR_HISTOGRAM, self.histogram_path, image,
                exception=e
            )
            raise FileErrorProblem(self.histogram_path)

        assert self.histogram.has_histogram_role()
        self.notify(
            ImportEventType.END_HISTOGRAM_DEPLOY, self.histogram_path, image
        )
        return self.histogram

    def mkdir(self, directory: Path):
        try:
            directory.mkdir()  # TODO: mode
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_ERROR, directory, exception=e)
            raise FileErrorProblem(directory)

    def move(self, origin: Path, dest: Path, prefer_copy: bool = False):
        try:
            if prefer_copy:
                shutil.copy(origin, dest)
            else:
                shutil.move(origin, dest)
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_NOT_MOVED, origin, exception=e)
            raise FileErrorProblem(origin)

    def mksymlink(self, path: Path, target: Path):
        try:
            path.symlink_to(
                target,
                target_is_directory=target.is_dir()
            )
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_ERROR, path, exception=e)
            raise FileErrorProblem(path)
