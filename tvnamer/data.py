import os
import re
import datetime
from typing import Optional, Dict, List, Union, Tuple

import tvdb_api

from tvnamer.config import Config
from tvnamer.tvnamer_exceptions import (
    InvalidPath,
    InvalidFilename,
    ShowNotFound,
    DataRetrievalError,
    SeasonNotFound,
    EpisodeNotFound,
    EpisodeNameNotFound,
    ConfigValueError,
    UserAbort,
)
from tvnamer.utils import (
    format_episode_name,
    format_episode_numbers,
    make_valid_filename,
    split_extension,
    _apply_replacements, # FIXME
)


def _replace_output_series_name(seriesname):
    # type: (str) -> str
    """transform TVDB series names

    after matching from TVDB, transform the series name for desired abbreviation, etc.

    This affects the output filename.
    """

    return Config['output_series_replacements'].get(seriesname, seriesname)



def _apply_replacements_output(cfile):
    # type: (str) -> str
    """Applies custom output filename replacements, wraps _apply_replacements
    """
    return _apply_replacements(cfile, Config['output_filename_replacements'])


def _apply_replacements_fullpath(cfile):
    # type: (str) -> str
    """Applies custom replacements to full path, wraps _apply_replacements
    """
    return _apply_replacements(cfile, Config['move_files_fullpath_replacements'])



class BaseInfo(object):
    """Base class for objects which store information (season, episode number, episode name), and contains
    logic to generate new name for each type of name
    """

    def __init__(
            self,
            filename, # type: Optional[str]
            extra,  # type: Optional[Dict[str, str]]
        ):
        # type: (...) -> None

        self.fullpath = filename
        if filename is not None:
            # Remains untouched, for use when renaming file
            self.originalfilename = os.path.basename(filename) # type: Optional[str]
        else:
            self.originalfilename = None

        if extra is None:
            extra = {}
        self.extra = extra

    def fullpath_get(self):
        return self._fullpath

    def fullpath_set(self, value):
        # type: (Optional[str]) -> None
        self._fullpath = value
        if value is None:
            self.filename, self.extension = None, None
        else:
            self.filepath, self.filename = os.path.split(value)
            self.filename, self.extension = split_extension(self.filename)

    fullpath = property(fullpath_get, fullpath_set)

    @property
    def fullfilename(self):
        return "%s%s" % (self.filename, self.extension)

    def getepdata(self):
        # type: () -> Dict[str, Optional[str]]
        raise NotImplemented

    def populate_from_tvdb(self, tvdb_instance, force_name=None, series_id=None):
        # ignore - type: (tvdb_api.Tvdb, Optional[Any], Optional[Any]) -> None
        """Queries the tvdb_api.Tvdb instance for episode name and corrected
        series name.
        If series cannot be found, it will warn the user. If the episode is not
        found, it will use the corrected show name and not set an episode name.
        If the site is unreachable, it will warn the user. If the user aborts
        it will catch tvdb_api's user abort error and raise tvnamer's
        """

        # FIXME: MOve this into each subclass - too much hasattr/isinstance
        try:
            if series_id is None:
                show = tvdb_instance[force_name or self.seriesname]
            else:
                series_id = int(series_id)
                tvdb_instance._getShowData(series_id, Config['language'])
                show = tvdb_instance[series_id]
        except tvdb_api.tvdb_error as errormsg:
            raise DataRetrievalError("Error with www.thetvdb.com: %s" % errormsg)
        except tvdb_api.tvdb_shownotfound:
            # No such series found.
            raise ShowNotFound("Show %s not found on www.thetvdb.com" % self.seriesname)
        except tvdb_api.tvdb_userabort as error:
            raise UserAbort("%s" % error)
        else:
            # Series was found, use corrected series name
            self.seriesname = _replace_output_series_name(show['seriesname'])

        if isinstance(self, DatedEpisodeInfo):
            # Date-based episode
            epnames = []
            for cepno in self.episodenumbers:
                try:
                    sr = show.aired_on(cepno)
                    if len(sr) > 1:
                        # filter out specials if multiple episodes aired on the day
                        sr = [s for s in sr if s['seasonnumber'] != '0']

                    if len(sr) > 1:
                        raise EpisodeNotFound(
                            "Ambigious air date %s, there were %s episodes on that day"
                            % (cepno, len(sr))
                        )
                    epnames.append(sr[0]['episodeName'])
                except tvdb_api.tvdb_episodenotfound:
                    raise EpisodeNotFound(
                        "Episode that aired on %s could not be found" % (cepno)
                    )
            self.episodename = epnames # Optional[List[str]]
            return

        if not hasattr(self, "seasonnumber") or self.seasonnumber is None:
            # Series without concept of seasons have all episodes in season 1
            seasonnumber = 1
        else:
            seasonnumber = self.seasonnumber

        epnames = []
        for cepno in self.episodenumbers:
            try:
                episodeinfo = show[seasonnumber][cepno]

            except tvdb_api.tvdb_seasonnotfound:
                raise SeasonNotFound(
                    "Season %s of show %s could not be found"
                    % (seasonnumber, self.seriesname)
                )

            except tvdb_api.tvdb_episodenotfound:
                # Try to search by absolute number
                sr = show.search(cepno, "absoluteNumber")
                if len(sr) > 1:
                    # For multiple results try and make sure there is a direct match
                    unsure = True
                    for e in sr:
                        if int(e['absoluteNumber']) == cepno:
                            epnames.append(e['episodeName'])
                            unsure = False
                    # If unsure error out
                    if unsure:
                        raise EpisodeNotFound(
                            "No episode actually matches %s, found %s results instead"
                            % (cepno, len(sr))
                        )
                elif len(sr) == 1:
                    epnames.append(sr[0]['episodeName'])
                else:
                    raise EpisodeNotFound(
                        "Episode %s of show %s, season %s could not be found (also tried searching by absolute episode number)"
                        % (cepno, self.seriesname, seasonnumber)
                    )

            except tvdb_api.tvdb_attributenotfound:
                raise EpisodeNameNotFound("Could not find episode name for %s" % cepno)
            else:
                epnames.append(episodeinfo['episodeName'])

        self.episodename = epnames

    def generate_filename(self, lowercase=False, preview_orig_filename=False):
        # type: (bool, bool) -> str

        # FIXME: MOve this into each subclass - too much hasattr/isinstance

        epdata = self.getepdata()

        # Add in extra dict keys, without clobbering existing values in epdata
        extra = self.extra.copy()
        extra.update(epdata)
        epdata = extra

        if self.episodename is None:
            fname = Config[self.CFG_KEY_WITHOUT_EP] % epdata
        else:
            if isinstance(self.episodename, list):
                epdata['episodename'] = format_episode_name(
                    self.episodename,
                    join_with=Config['multiep_join_name_with'],
                    multiep_format=Config['multiep_format'],
                )
            fname = Config[self.CFG_KEY_WITH_EP] % epdata

        if Config['titlecase_filename']:
            from tvnamer._titlecase import titlecase

            fname = titlecase(fname)

        if lowercase or Config['lowercase_filename']:
            fname = fname.lower()

        if preview_orig_filename:
            # Return filename without custom replacements or filesystem-validness
            return fname

        if len(Config['output_filename_replacements']) > 0:
            fname = _apply_replacements_output(fname)

        return make_valid_filename(
            fname,
            normalize_unicode=Config['normalize_unicode_filenames'],
            windows_safe=Config['windows_safe_filenames'],
            custom_blacklist=Config['custom_filename_character_blacklist'],
            replace_with=Config['replace_invalid_characters_with'],
        )


class EpisodeInfo(BaseInfo):

    CFG_KEY_WITH_EP = "filename_with_episode"
    CFG_KEY_WITHOUT_EP = "filename_without_episode"

    def __init__(
        self,
        seriesname,  # type: str
        seasonnumber,  # type: int
        episodenumbers,  # type: List[int]
        episodename=None,  # type: Union[List[Optional[str]], Optional[str]]
        filename=None,  # type: Optional[str]
        extra=None,  # type: Optional[Dict[str, str]]
        ):
        # type: (...) -> None

        super(EpisodeInfo, self).__init__(filename=filename, extra=extra)

        self.seriesname = seriesname
        self.seasonnumber = seasonnumber
        self.episodenumbers = episodenumbers
        self.episodename = episodename

    def sortable_info(self):
        # type: () -> Tuple[str, int, List[int]]
        """Returns a tuple of sortable information
        """
        return ("%s" % self.seriesname, self.seasonnumber, self.episodenumbers)

    def number_string(self):
        # type: () -> str
        """Used in UI
        """
        return "season: %s, episode: %s" % (
            self.seasonnumber,
            ", ".join([str(x) for x in self.episodenumbers]),
        )

    def getepdata(self):
        # type: () -> Dict[str, Optional[str]]
        """
        Uses the following config options:
        filename_with_episode # Filename when episode name is found
        filename_without_episode # Filename when no episode can be found
        episode_single # formatting for a single episode number
        episode_separator # used to join multiple episode numbers
        """
        # Format episode number into string, or a list
        epno = format_episode_numbers(self.episodenumbers)

        # Data made available to config'd output file format
        if self.extension is None:
            prep_extension = ''
        else:
            prep_extension = self.extension

        epdata = {
            'seriesname': self.seriesname,
            'seasonno': self.seasonnumber,  # TODO: deprecated attribute, make this warn somehow
            'seasonnumber': self.seasonnumber,
            'episode': epno,
            'episodename': self.episodename,
            'ext': prep_extension,
        }

        return epdata

    def __repr__(self):
        return "<%s: %r>" % (self.__class__.__name__, self.generate_filename())


class DatedEpisodeInfo(BaseInfo):
    CFG_KEY_WITH_EP = "filename_with_date_and_episode"
    CFG_KEY_WITHOUT_EP = "filename_with_date_without_episode"

    def __init__(
        self,
        seriesname,  # type: str
        episodenumbers,  # type: List[datetime.date]
        episodename=None,  # type: Optional[List[str]]
        filename=None,  # type: Optional[str]
        extra=None,  # type: Optional[Dict[str, str]]
        ):
        # type: (...) -> None

        self.seriesname = seriesname
        self.episodenumbers = episodenumbers
        self.episodename = episodename # type: Optional[List[str]]
        self.fullpath = filename

        if filename is not None:
            # Remains untouched, for use when renaming file
            self.originalfilename = os.path.basename(filename)
        else:
            self.originalfilename = None

        if filename is not None:
            # Remains untouched, for use when renaming file
            self.originalfilename = os.path.basename(filename)
        else:
            self.originalfilename = None

        if extra is None:
            extra = {}
        self.extra = extra

    def sortable_info(self):
        """Returns a tuple of sortable information
        """
        return ("%s" % (self.seriesname), self.episodenumbers)

    def number_string(self):
        """Used in UI
        """
        return "episode: %s" % (", ".join([str(x) for x in self.episodenumbers]))

    def getepdata(self):
        # type: () -> Dict[str, Optional[str]]
        # Format episode number into string, or a list

        if self.episodename is None:
            prep_episodename = None # type: Optional[str]
        if isinstance(self.episodename, list):
            prep_episodename = format_episode_name(
                self.episodename,
                join_with=Config['multiep_join_name_with'],
                multiep_format=Config['multiep_format'],
            )
        else:
            prep_episodename = self.episodename

        # Data made available to config'd output file format
        if self.extension is None:
            prep_extension = ''
        else:
            prep_extension = self.extension

        dates = str(self.episodenumbers[0])

        epdata = {
            'seriesname': self.seriesname,
            'episode': dates,
            'episodename': prep_episodename,
            'ext': prep_extension,
        }

        return epdata


class NoSeasonEpisodeInfo(BaseInfo):
    CFG_KEY_WITH_EP = "filename_with_episode_no_season"
    CFG_KEY_WITHOUT_EP = "filename_without_episode_no_season"

    def __init__(
        self,
        seriesname,  # type: str
        episodenumbers,  # type: List[int]
        episodename=None,  # type: Optional[str]
        filename=None,  # type: Optional[str]
        extra=None,  # type: Optional[Dict[str, str]]
        ):
        # type: (...) -> None

        self.seriesname = seriesname
        self.episodenumbers = episodenumbers
        self.episodename = episodename
        self.fullpath = filename

        if filename is not None:
            # Remains untouched, for use when renaming file
            self.originalfilename = os.path.basename(filename)
        else:
            self.originalfilename = None

        if extra is None:
            extra = {}
        self.extra = extra

    def sortable_info(self):
        """Returns a tuple of sortable information
        """
        return ("%s" % self.seriesname, self.episodenumbers)

    def number_string(self):
        """Used in UI
        """
        return "episode: %s" % (", ".join([str(x) for x in self.episodenumbers]))

    def getepdata(self):
        # type: () -> Dict[str, Optional[str]]
        epno = format_episode_numbers(self.episodenumbers)

        # Data made available to config'd output file format
        if self.extension is None:
            prep_extension = ''
        else:
            prep_extension = self.extension

        epdata = {
            'seriesname': self.seriesname,
            'episode': epno,
            'episodename': self.episodename,
            'ext': prep_extension,
        }

        return epdata


class AnimeEpisodeInfo(NoSeasonEpisodeInfo):
    CFG_KEY_WITH_EP = "filename_anime_with_episode"
    CFG_KEY_WITHOUT_EP = "filename_anime_without_episode"

    CFG_KEY_WITH_EP_NO_CRC = "filename_anime_with_episode_without_crc"
    CFG_KEY_WITHOUT_EP_NO_CRC = "filename_anime_without_episode_without_crc"

    def generate_filename(self, lowercase=False, preview_orig_filename=False):
        epdata = self.getepdata()

        # Add in extra dict keys, without clobbering existing values in epdata
        extra = self.extra.copy()
        extra.update(epdata)
        epdata = extra

        # Get appropriate config key, depending on if episode name was
        # found, and if crc value was found
        if self.episodename is None:
            if self.extra.get('crc') is None:
                cfgkey = self.CFG_KEY_WITHOUT_EP_NO_CRC
            else:
                # Have crc, but no ep name
                cfgkey = self.CFG_KEY_WITHOUT_EP
        else:
            if self.extra.get('crc') is None:
                cfgkey = self.CFG_KEY_WITH_EP_NO_CRC
            else:
                cfgkey = self.CFG_KEY_WITH_EP

        if self.episodename is not None:
            if isinstance(self.episodename, list):
                epdata['episodename'] = format_episode_name(
                    self.episodename,
                    join_with=Config['multiep_join_name_with'],
                    multiep_format=Config['multiep_format'],
                )

        fname = Config[cfgkey] % epdata

        if lowercase or Config['lowercase_filename']:
            fname = fname.lower()

        if preview_orig_filename:
            # Return filename without custom replacements or filesystem-validness
            return fname

        if len(Config['output_filename_replacements']) > 0:
            fname = _apply_replacements_output(fname)

        return make_valid_filename(
            fname,
            normalize_unicode=Config['normalize_unicode_filenames'],
            windows_safe=Config['windows_safe_filenames'],
            custom_blacklist=Config['custom_filename_character_blacklist'],
            replace_with=Config['replace_invalid_characters_with'],
        )
