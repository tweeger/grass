"""!
@package mapdisp.frame

@brief Map display with toolbar for various display management
functions, and additional toolbars (vector digitizer, 3d view).

Can be used either from Layer Manager or as d.mon backend.

Classes:
 - mapdisp::MapFrame

(C) 2006-2012 by the GRASS Development Team

This program is free software under the GNU General Public License
(>=v2). Read the file COPYING that comes with GRASS for details.

@author Michael Barton
@author Jachym Cepicky
@author Martin Landa <landa.martin gmail.com>
@author Vaclav Petras <wenzeslaus gmail.com> (SingleMapFrame, handlers support)
@author Anna Kratochvilova <kratochanna gmail.com> (SingleMapFrame)
@author Stepan Turek <stepan.turek seznam.cz> (handlers support)
"""

import os
import sys
import math
import copy

from core import globalvar
import wx
import wx.aui

sys.path.append(os.path.join(globalvar.ETCWXDIR, "icons"))
sys.path.append(os.path.join(globalvar.ETCDIR,   "python"))

from core               import globalvar
from core.render        import Map
from core.ws            import EVT_UPDATE_PRGBAR
from vdigit.toolbars    import VDigitToolbar
from mapdisp.toolbars   import MapToolbar, NvizIcons
from mapdisp.gprint     import PrintOptions
from core.gcmd          import GError, GMessage
from dbmgr.dialogs      import DisplayAttributesDialog
from core.utils         import ListOfCatsToRange, GetLayerNameFromCmd
from gui_core.dialogs   import GetImageHandlers, ImageSizeDialog, DecorationDialog, TextLayerDialog
from core.debug         import Debug
from core.settings      import UserSettings
from gui_core.mapdisp   import SingleMapFrame
from mapdisp.mapwindow  import BufferedWindow
from modules.histogram  import HistogramFrame
from wxplot.histogram   import Histogram2Frame
from wxplot.profile     import ProfileFrame
from wxplot.scatter     import ScatterFrame

from mapdisp import statusbar as sb

import grass.script as grass

haveCtypes = False

class MapFrame(SingleMapFrame):
    """!Main frame for map display window. Drawing takes place in
    child double buffered drawing window.
    """
    def __init__(self, parent, giface, title = _("GRASS GIS - Map display"),
                 toolbars = ["map"], tree = None, notebook = None, lmgr = None,
                 page = None, Map = Map(), auimgr = None, name = 'MapWindow', **kwargs):
        """!Main map display window with toolbars, statusbar and
        BufferedWindow (map canvas)
        
        @param toolbars array of activated toolbars, e.g. ['map', 'digit']
        @param tree reference to layer tree
        @param notebook control book ID in Layer Manager
        @param lmgr Layer Manager
        @param page notebook page with layer tree
        @param Map instance of render.Map
        @param auimgs AUI manager
        @param name frame name
        @param kwargs wx.Frame attributes
        """
        SingleMapFrame.__init__(self, parent = parent, title = title,
                              Map = Map, auimgr = auimgr, name = name, **kwargs)
        
        self._giface = giface
        self._layerManager = lmgr   # Layer Manager object
        self.tree       = tree      # Layer Manager layer tree object
        self.page       = page      # Notebook page holding the layer tree
        self.layerbook  = notebook  # Layer Manager layer tree notebook
        #
        # Add toolbars
        #
        for toolb in toolbars:
            self.AddToolbar(toolb)
        
        #
        # Add statusbar
        #
        
        # items for choice
        self.statusbarItems = [sb.SbCoordinates,
                               sb.SbRegionExtent,
                               sb.SbCompRegionExtent,
                               sb.SbShowRegion,
                               sb.SbAlignExtent,
                               sb.SbResolution,
                               sb.SbDisplayGeometry,
                               sb.SbMapScale,
                               sb.SbGoTo,
                               sb.SbProjection]
                            
        self.statusbarItemsHiddenInNviz = (sb.SbAlignExtent,
                                           sb.SbDisplayGeometry,
                                           sb.SbShowRegion,
                                           sb.SbResolution,
                                           sb.SbMapScale)
        
        # create statusbar and its manager
        statusbar = self.CreateStatusBar(number = 4, style = 0)
        statusbar.SetStatusWidths([-5, -2, -1, -1])
        self.statusbarManager = sb.SbManager(mapframe = self, statusbar = statusbar)
        
        # fill statusbar manager
        self.statusbarManager.AddStatusbarItemsByClass(self.statusbarItems, mapframe = self, statusbar = statusbar)
        self.statusbarManager.AddStatusbarItem(sb.SbMask(self, statusbar = statusbar, position = 2))
        self.statusbarManager.AddStatusbarItem(sb.SbRender(self, statusbar = statusbar, position = 3))
        
        self.statusbarManager.Update()

        #
        # Init map display (buffered DC & set default cursor)
        #
        self.MapWindow2D = BufferedWindow(self, giface = self._giface, id = wx.ID_ANY,
                                          Map = self.Map, frame = self, tree = self.tree, lmgr = self._layerManager)
        # default is 2D display mode
        self.MapWindow = self.MapWindow2D
        self.MapWindow.SetCursor(self.cursors["default"])
        # used by vector digitizer
        self.MapWindowVDigit = None
        # used by Nviz (3D display mode)
        self.MapWindow3D = None 

        #
        # initialize region values
        #
        self._initMap(Map = self.Map) 

        #
        # Bind various events
        #
        self.Bind(wx.EVT_ACTIVATE, self.OnFocus)
        self.Bind(wx.EVT_CLOSE,    self.OnCloseWindow)
        self.Bind(EVT_UPDATE_PRGBAR, self.OnUpdateProgress)
        
        #
        # Update fancy gui style
        #
        self._mgr.AddPane(self.MapWindow, wx.aui.AuiPaneInfo().CentrePane().
                          Dockable(False).BestSize((-1,-1)).Name('2d').
                          CloseButton(False).DestroyOnClose(True).
                          Layer(0))
        self._mgr.Update()

        #
        # Init print module and classes
        #
        self.printopt = PrintOptions(self, self.MapWindow)
        
        #
        # Init zoom history
        #
        self.MapWindow.ZoomHistory(self.Map.region['n'],
                                   self.Map.region['s'],
                                   self.Map.region['e'],
                                   self.Map.region['w'])

        #
        # Re-use dialogs
        #
        self.dialogs = {}
        self.dialogs['attributes'] = None
        self.dialogs['category'] = None
        self.dialogs['barscale'] = None
        self.dialogs['legend'] = None
        self.dialogs['vnet'] = None

        self.decorationDialog = None # decoration/overlays
        
    def GetMapWindow(self):
        return self.MapWindow
    
    def _addToolbarVDigit(self):
        """!Add vector digitizer toolbar
        """
        from vdigit.main import haveVDigit, VDigit
        
        if not haveVDigit:
            from vdigit import errorMsg
            
            self.toolbars['map'].combo.SetValue(_("2D view"))
            
            GError(_("Unable to start wxGUI vector digitizer.\n"
                     "Details: %s") % errorMsg, parent = self)
            return
        
        if self._layerManager:
            log = self._layerManager.GetLogWindow()
        else:
            log = None
        
        if not self.MapWindowVDigit:
            from vdigit.mapwindow import VDigitWindow
            self.MapWindowVDigit = VDigitWindow(parent = self, giface = self._giface,
                                                id = wx.ID_ANY, frame = self,
                                                Map = self.Map, tree = self.tree,
                                                lmgr = self._layerManager)
            self.MapWindowVDigit.Show()
            self._mgr.AddPane(self.MapWindowVDigit, wx.aui.AuiPaneInfo().CentrePane().
                          Dockable(False).BestSize((-1,-1)).Name('vdigit').
                          CloseButton(False).DestroyOnClose(True).
                          Layer(0))
        
        self.MapWindow = self.MapWindowVDigit
        
        if self._mgr.GetPane('2d').IsShown():
            self._mgr.GetPane('2d').Hide()
        elif self._mgr.GetPane('3d').IsShown():
            self._mgr.GetPane('3d').Hide()
        self._mgr.GetPane('vdigit').Show()
        self.toolbars['vdigit'] = VDigitToolbar(parent = self, MapWindow = self.MapWindow,
                                                digitClass = VDigit, giface =  self._giface,
                                                layerTree = self.tree, log = log)
        self.MapWindowVDigit.SetToolbar(self.toolbars['vdigit'])
        
        self._mgr.AddPane(self.toolbars['vdigit'],
                          wx.aui.AuiPaneInfo().
                          Name("vdigittoolbar").Caption(_("Vector Digitizer Toolbar")).
                          ToolbarPane().Top().Row(1).
                          LeftDockable(False).RightDockable(False).
                          BottomDockable(False).TopDockable(True).
                          CloseButton(False).Layer(2).
                          BestSize((self.toolbars['vdigit'].GetBestSize())))
        # change mouse to draw digitized line
        self.MapWindow.mouse['box'] = "point"
        self.MapWindow.zoomtype     = 0
        self.MapWindow.pen          = wx.Pen(colour = 'red',   width = 2, style = wx.SOLID)
        self.MapWindow.polypen      = wx.Pen(colour = 'green', width = 2, style = wx.SOLID)

    def AddNviz(self):
        """!Add 3D view mode window
        """
        from nviz.main import haveNviz, GLWindow, errorMsg
        
        # check for GLCanvas and OpenGL
        if not haveNviz:
            self.toolbars['map'].combo.SetValue(_("2D view"))
            GError(parent = self,
                   message = _("Unable to switch to 3D display mode.\nThe Nviz python extension "
                               "was not found or loaded properly.\n"
                               "Switching back to 2D display mode.\n\nDetails: %s" % errorMsg))
            return
        
        # disable 3D mode for other displays
        for page in range(0, self._layerManager.GetLayerNotebook().GetPageCount()):
            mapdisp = self._layerManager.GetLayerNotebook().GetPage(page).maptree.GetMapDisplay()
            if self._layerManager.GetLayerNotebook().GetPage(page) != self._layerManager.currentPage:
                if '3D' in mapdisp.toolbars['map'].combo.GetString(1):
                    mapdisp.toolbars['map'].combo.Delete(1)
        self.toolbars['map'].Enable2D(False)
        # add rotate tool to map toolbar
        self.toolbars['map'].InsertTool((('rotate', NvizIcons['rotate'],
                                          self.OnRotate, wx.ITEM_CHECK, 7),)) # 7 is position
        self.toolbars['map'].InsertTool((('flyThrough', NvizIcons['flyThrough'],
                                          self.OnFlyThrough, wx.ITEM_CHECK, 8),)) 
        self.toolbars['map'].ChangeToolsDesc(mode2d = False)
        # update status bar
        
        self.statusbarManager.HideStatusbarChoiceItemsByClass(self.statusbarItemsHiddenInNviz)
        self.statusbarManager.SetMode(0)
        
        # erase map window
        self.MapWindow.EraseMap()
        
        self._giface.WriteCmdLog(_("Starting 3D view mode..."))
        self.SetStatusText(_("Please wait, loading data..."), 0)
        
        # create GL window
        if not self.MapWindow3D:
            self.MapWindow3D = GLWindow(self, giface = self._giface, id = wx.ID_ANY, frame = self,
                                        Map = self.Map, tree = self.tree, lmgr = self._layerManager)
            self.MapWindow = self.MapWindow3D
            self.MapWindow.SetCursor(self.cursors["default"])
            
            # add Nviz notebookpage
            self._layerManager.AddNvizTools()
            
            # switch from MapWindow to MapWindowGL
            self._mgr.GetPane('2d').Hide()
            self._mgr.AddPane(self.MapWindow3D, wx.aui.AuiPaneInfo().CentrePane().
                              Dockable(False).BestSize((-1,-1)).Name('3d').
                              CloseButton(False).DestroyOnClose(True).
                              Layer(0))
            
            self.MapWindow3D.Show()
            self.MapWindow3D.ResetViewHistory()            
            self.MapWindow3D.UpdateView(None)
        else:
            self.MapWindow = self.MapWindow3D
            os.environ['GRASS_REGION'] = self.Map.SetRegion(windres = True, windres3 = True)
            self.MapWindow3D.GetDisplay().Init()
            del os.environ['GRASS_REGION']
            
            # switch from MapWindow to MapWindowGL
            self._mgr.GetPane('2d').Hide()
            self._mgr.GetPane('3d').Show()
            
            # add Nviz notebookpage
            self._layerManager.AddNvizTools()
            self.MapWindow3D.ResetViewHistory()
            for page in ('view', 'light', 'fringe', 'constant', 'cplane', 'animation'):
                self._layerManager.nviz.UpdatePage(page)
                
        self.MapWindow3D.overlays = self.MapWindow2D.overlays
        self.MapWindow3D.textdict = self.MapWindow2D.textdict
        # update overlays needs to be called after because getClientSize
        # is called during update and it must give reasonable values
        wx.CallAfter(self.MapWindow3D.UpdateOverlays)
        
        self.SetStatusText("", 0)
        self._mgr.Update()
    
    def RemoveNviz(self):
        """!Restore 2D view"""
        try:
            self.toolbars['map'].RemoveTool(self.toolbars['map'].rotate)
            self.toolbars['map'].RemoveTool(self.toolbars['map'].flyThrough)
        except AttributeError:
            pass
        
        # update status bar
        self.statusbarManager.ShowStatusbarChoiceItemsByClass(self.statusbarItemsHiddenInNviz)
        self.statusbarManager.SetMode(UserSettings.Get(group = 'display',
                                                       key = 'statusbarMode',
                                                       subkey = 'selection'))
        self.SetStatusText(_("Please wait, unloading data..."), 0)
        self._giface.WriteCmdLog(_("Switching back to 2D view mode..."))
        if self.MapWindow3D:
            self.MapWindow3D.OnClose(event = None)
        # switch from MapWindowGL to MapWindow
        self._mgr.GetPane('2d').Show()
        self._mgr.GetPane('3d').Hide()

        self.MapWindow = self.MapWindow2D
        # remove nviz notebook page
        self._layerManager.RemoveNvizTools()
        try:
            self.MapWindow2D.overlays = self.MapWindow3D.overlays
            self.MapWindow2D.textdict = self.MapWindow3D.textdict
        except AttributeError:
            pass
        self.MapWindow.UpdateMap()
        self._mgr.Update()
        
    def AddToolbar(self, name, fixed = False):
        """!Add defined toolbar to the window
        
        Currently known toolbars are:
         - 'map'     - basic map toolbar
         - 'vdigit'  - vector digitizer
         - 'gcpdisp' - GCP Manager 
         
        @param name toolbar to add
        @param fixed fixed toolbar
        """
        # default toolbar
        if name == "map":
            self.toolbars['map'] = MapToolbar(self, self.Map)
            
            self._mgr.AddPane(self.toolbars['map'],
                              wx.aui.AuiPaneInfo().
                              Name("maptoolbar").Caption(_("Map Toolbar")).
                              ToolbarPane().Top().Name('mapToolbar').
                              LeftDockable(False).RightDockable(False).
                              BottomDockable(False).TopDockable(True).
                              CloseButton(False).Layer(2).
                              BestSize((self.toolbars['map'].GetBestSize())))
            
        # vector digitizer
        elif name == "vdigit":
            self.toolbars['map'].combo.SetValue(_("Digitize"))
            self._addToolbarVDigit()
        
        if fixed:
            self.toolbars['map'].combo.Disable()
         
        self._mgr.Update()
        
    def RemoveToolbar (self, name):
        """!Removes defined toolbar from the window

        @todo Only hide, activate by calling AddToolbar()
        """
        # cannot hide main toolbar
        if name == "map":
            return
        
        self._mgr.DetachPane(self.toolbars[name])
        self.toolbars[name].Destroy()
        self.toolbars.pop(name)
        
        if name == 'vdigit':
            self._mgr.GetPane('vdigit').Hide()
            self._mgr.GetPane('2d').Show()
            self.MapWindow = self.MapWindow2D
            
        self.toolbars['map'].combo.SetValue(_("2D view"))
        self.toolbars['map'].Enable2D(True)
        
        self._mgr.Update()
    
    def IsPaneShown(self, name):
        """!Check if pane (toolbar, mapWindow ...) of given name is currently shown"""
        if self._mgr.GetPane(name).IsOk():
            return self._mgr.GetPane(name).IsShown()
        return False
        
    def OnUpdateProgress(self, event):
        """!Update progress bar info
        """
        self.GetProgressBar().UpdateProgress(event.layer, event.map)
        
        event.Skip()
        
    def OnFocus(self, event):
        """!Change choicebook page to match display.
        """
        # change bookcontrol page to page associated with display
        if self.page:
            pgnum = self.layerbook.GetPageIndex(self.page)
            if pgnum > -1:
                self.layerbook.SetSelection(pgnum)
                self._layerManager.currentPage = self.layerbook.GetCurrentPage()
        
        event.Skip()
        
    def RemoveQueryLayer(self):
        """!Removes temporary map layers (queries)"""
        qlayer = self.GetMap().GetListOfLayers(name = globalvar.QUERYLAYER)
        for layer in qlayer:
            self.GetMap().DeleteLayer(layer)

    def OnRender(self, event):
        """!Re-render map composition (each map layer)
        """
        self.RemoveQueryLayer()
        
        # delete tmp lines
        if self.MapWindow.mouse["use"] in ("measure",
                                           "profile"):
            self.MapWindow.polycoords = []
            self.MapWindow.ClearLines()
        
        # deselect features in vdigit
        if self.GetToolbar('vdigit'):
            if self.MapWindow.digit:
                self.MapWindow.digit.GetDisplay().SetSelected([])
            self.MapWindow.UpdateMap(render = True, renderVector = True)
        else:
            self.MapWindow.UpdateMap(render = True)
        
        # update statusbar
        self.StatusbarUpdate()

    def OnPointer(self, event):
        """!Pointer button clicked
        """
        if self.GetMapToolbar():
            if event:
                self.SwitchTool(self.toolbars['map'], event)
            self.toolbars['map'].action['desc'] = ''
        
        self.MapWindow.mouse['use'] = "pointer"
        self.MapWindow.mouse['box'] = "point"

        # change the cursor
        if self.GetToolbar('vdigit'):
            # digitization tool activated
            self.MapWindow.SetCursor(self.cursors["cross"])

            # reset mouse['box'] if needed
            if self.toolbars['vdigit'].GetAction() in ['addLine']:
                if self.toolbars['vdigit'].GetAction('type') in ['point', 'centroid']:
                    self.MapWindow.mouse['box'] = 'point'
                else: # line, boundary
                    self.MapWindow.mouse['box'] = 'line'
            elif self.toolbars['vdigit'].GetAction() in ['addVertex', 'removeVertex', 'splitLine',
                                                         'editLine', 'displayCats', 'queryMap',
                                                         'copyCats']:
                self.MapWindow.mouse['box'] = 'point'
            else: # moveLine, deleteLine
                self.MapWindow.mouse['box'] = 'box'
        
        else:
            self.MapWindow.SetCursor(self.cursors["default"])

    def OnRotate(self, event):
        """!Rotate 3D view
        """
        if self.GetMapToolbar():
            self.SwitchTool(self.toolbars['map'], event)
            self.toolbars['map'].action['desc'] = ''
        
        self.MapWindow.mouse['use'] = "rotate"
        
        # change the cursor
        self.MapWindow.SetCursor(self.cursors["hand"])

    def OnFlyThrough(self, event):
        """!Fly-through mode
        """
        if self.GetMapToolbar():
            self.SwitchTool(self.toolbars['map'], event)
            self.toolbars['map'].action['desc'] = ''
        
        self.MapWindow.mouse['use'] = "fly"
        
        # change the cursor
        self.MapWindow.SetCursor(self.cursors["hand"])
        self.MapWindow.SetFocus()
        
    def OnZoomRegion(self, event):
        """!Zoom to region
        """
        self.Map.getRegion()
        self.Map.getResolution()
        self.UpdateMap()
        # event.Skip()

    def OnAlignRegion(self, event):
        """!Align region
        """
        if not self.Map.alignRegion:
            self.Map.alignRegion = True
        else:
            self.Map.alignRegion = False
        # event.Skip()        
        
    def SaveToFile(self, event):
        """!Save map to image
        """
        if self.IsPaneShown('3d'):
            filetype = "TIF file (*.tif)|*.tif|PPM file (*.ppm)|*.ppm"
            ltype = [{ 'ext' : 'tif', 'type' : 'tif' },
                     { 'ext' : 'ppm', 'type' : 'ppm' }]
        else:
            img = self.MapWindow.img
            if not img:
                GMessage(parent = self,
                         message = _("Nothing to render (empty map). Operation canceled."))
                return
            filetype, ltype = GetImageHandlers(img)
        
        # get size
        dlg = ImageSizeDialog(self)
        dlg.CentreOnParent()
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        width, height = dlg.GetValues()
        dlg.Destroy()
        
        # get filename
        dlg = wx.FileDialog(parent = self,
                            message = _("Choose a file name to save the image "
                                        "(no need to add extension)"),
                            wildcard = filetype,
                            style = wx.SAVE | wx.FD_OVERWRITE_PROMPT)
        
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            if not path:
                dlg.Destroy()
                return
            
            base, ext = os.path.splitext(path)
            fileType = ltype[dlg.GetFilterIndex()]['type']
            extType  = ltype[dlg.GetFilterIndex()]['ext']
            if ext != extType:
                path = base + '.' + extType
            
            self.MapWindow.SaveToFile(path, fileType,
                                      width, height)
            
        dlg.Destroy()

    def PrintMenu(self, event):
        """
        Print options and output menu for map display
        """
        printmenu = wx.Menu()
        # Add items to the menu
        setup = wx.MenuItem(printmenu, wx.ID_ANY, _('Page setup'))
        printmenu.AppendItem(setup)
        self.Bind(wx.EVT_MENU, self.printopt.OnPageSetup, setup)

        preview = wx.MenuItem(printmenu, wx.ID_ANY, _('Print preview'))
        printmenu.AppendItem(preview)
        self.Bind(wx.EVT_MENU, self.printopt.OnPrintPreview, preview)

        doprint = wx.MenuItem(printmenu, wx.ID_ANY, _('Print display'))
        printmenu.AppendItem(doprint)
        self.Bind(wx.EVT_MENU, self.printopt.OnDoPrint, doprint)

        # Popup the menu.  If an item is selected then its handler
        # will be called before PopupMenu returns.
        self.PopupMenu(printmenu)
        printmenu.Destroy()

    def OnCloseWindow(self, event):
        """!Window closed.
        Also close associated layer tree page
        """
        pgnum = None
        self.Map.Clean()
        
        # close edited map and 3D tools properly
        if self.GetToolbar('vdigit'):
            maplayer = self.toolbars['vdigit'].GetLayer()
            if maplayer:
                self.toolbars['vdigit'].OnExit()
        if self.IsPaneShown('3d'):
            self.RemoveNviz()
        
        if not self._layerManager:
            self.Destroy()
        elif self.page:
            pgnum = self.layerbook.GetPageIndex(self.page)
            if pgnum > -1:
                self.layerbook.DeletePage(pgnum)

    def Query(self, x, y):
        """!Query selected layers. 

        Calls QueryMap in case of raster or more vectors,
        or QueryVector in case of one vector with db connection.

        @param x,y coordinates
        @param layers selected tree item layers
        """
        layers = self._giface.GetLayerList().GetSelectedLayers(checkedOnly = True)
        filteredLayers = []
        for layer in layers:
            ltype = layer.maplayer.GetType()
            if ltype in ('raster', 'rgb', 'his',
                         'vector', 'thememap', 'themechart'):
                filteredLayers.append(layer)

        if not filteredLayers:
            GMessage(parent = self,
                     message = _('No raster or vector map layer selected for querying.'))
            return
            
        layers = filteredLayers
        # set query snap distance for v.what at map unit equivalent of 10 pixels
        qdist = 10.0 * ((self.Map.region['e'] - self.Map.region['w']) / self.Map.width)

        # TODO: replace returning None by exception or so
        try:
            east, north = self.MapWindow.Pixel2Cell((x, y))
        except TypeError:
            return

        posWindow = self.ClientToScreen((x + self.MapWindow.dialogOffset,
                                         y + self.MapWindow.dialogOffset))

        isRaster = False
        nVectors = 0
        isDbConnection = False
        for l in layers:
            maplayer = l.maplayer
            if maplayer.GetType() == 'raster':
                isRaster = True
                break
            if maplayer.GetType() == 'vector':
                nVectors += 1
                isDbConnection = grass.vector_db(maplayer.GetName())

        if not self.IsPaneShown('3d'):
            if isRaster or nVectors > 1 or not isDbConnection:
                self.QueryMap(east, north, qdist, layers)
            else:
                self.QueryVector(east, north, qdist, posWindow, layers[0])
        else:
            if isRaster:
                self.MapWindow.QuerySurface(x, y)
            if nVectors > 1 or not isDbConnection:
                self.QueryMap(east, north, qdist, layers)
            elif nVectors == 1:
                self.QueryVector(east, north, qdist, posWindow, layers[0])

    def QueryMap(self, east, north, qdist, layers):
        """!Query raster or vector map layers by r/v.what
        
        @param east,north coordinates
        @param qdist query distance
        @param layers selected tree items
        """
        rast = list()
        vect = list()
        rcmd = ['r.what', '--v']
        vcmd = ['v.what', '--v']
        
        for layer in layers:
            ltype = layer.maplayer.GetType()
            dcmd = layer.cmd
            name, found = GetLayerNameFromCmd(dcmd)
            
            if not found:
                continue
            if ltype == 'raster':
                rast.append(name)
            elif ltype in ('rgb', 'his'):
                for iname in name.split('\n'):
                    rast.append(iname)
            elif ltype in ('vector', 'thememap', 'themechart'):
                vect.append(name)

        # use display region settings instead of computation region settings
        self.tmpreg = os.getenv("GRASS_REGION")
        os.environ["GRASS_REGION"] = self.Map.SetRegion(windres = False)
        
        # build query commands for any selected rasters and vectors
        if rast:
            rcmd.append('-f')
            rcmd.append('-n')
            rcmd.append('map=%s' % ','.join(rast))
            rcmd.append('coordinates=%f,%f' % (float(east), float(north)))
        
        if vect:
            # check for vector maps open to be edited
            digitToolbar = self.GetToolbar('vdigit')
            if digitToolbar:
                lmap = digitToolbar.GetLayer().GetName()
                for name in vect:
                    if lmap == name:
                        self._giface.WriteWarning(_("Vector map <%s> "
                                                                  "opened for editing - skipped.") % lmap)
                        vect.remove(name)
            
            if len(vect) < 1:
                self._giface.WriteCmdLog(_("Nothing to query."))
                return
            
            vcmd.append('-a')
            vcmd.append('map=%s' % ','.join(vect))
            vcmd.append('layer=%s' % ','.join(['-1'] * len(vect)))
            vcmd.append('coordinates=%f,%f' % (float(east), float(north)))
            vcmd.append('distance=%f' % float(qdist))
        
        Debug.msg(1, "QueryMap(): raster=%s vector=%s" % (','.join(rast),
                                                          ','.join(vect)))
        # parse query command(s)

        if rast and not self.IsPaneShown('3d'):
            self._giface.RunCmd(rcmd,
                                compReg = False,
                                onDone  =  self._QueryMapDone)
        if vect:
            self._giface.RunCmd(vcmd, onDone = self._QueryMapDone)
        
    def _QueryMapDone(self, cmd, returncode):
        """!Restore settings after querying (restore GRASS_REGION)
        
        @param returncode command return code
        """
        if hasattr(self, "tmpreg"):
            if self.tmpreg:
                os.environ["GRASS_REGION"] = self.tmpreg
            elif 'GRASS_REGION' in os.environ:
                del os.environ["GRASS_REGION"]
        elif 'GRASS_REGION' in os.environ:
            del os.environ["GRASS_REGION"]
        
        if hasattr(self, "tmpreg"):
            del self.tmpreg
        
    def QueryVector(self, east, north, qdist, posWindow, layer):
        """!Query vector map layer features

        Attribute data of selected vector object are displayed in GUI dialog.
        Data can be modified (On Submit)
        """
        mapName = layer.maplayer.name
        
        if layer.maplayer.GetMapset() != \
                grass.gisenv()['MAPSET']:
            mode = 'display'
        else:
            mode = 'update'
        
        if self.dialogs['attributes'] is None:
            dlg = DisplayAttributesDialog(parent = self.MapWindow,
                                                      map = mapName,
                                                      query = ((east, north), qdist),
                                                      pos = posWindow,
                                                      action = mode)
            self.dialogs['attributes'] = dlg
        
        else:
            # selection changed?
            if not self.dialogs['attributes'].mapDBInfo or \
                    self.dialogs['attributes'].mapDBInfo.map != mapName:
                self.dialogs['attributes'].UpdateDialog(map = mapName, query = ((east, north), qdist),
                                                        action = mode)
            else:
                self.dialogs['attributes'].UpdateDialog(query = ((east, north), qdist),
                                                        action = mode)
        if not self.dialogs['attributes'].IsFound():
            self._giface.WriteLog(_('Nothing found.'))
        
        cats = self.dialogs['attributes'].GetCats()
        
        qlayer = None
        if not self.IsPaneShown('3d') and self.IsAutoRendered():
            try:
                qlayer = self.Map.GetListOfLayers(name = globalvar.QUERYLAYER)[0]
            except IndexError:
                pass
        
        if self.dialogs['attributes'].mapDBInfo and cats:
            if not self.IsPaneShown('3d') and self.IsAutoRendered():
                # highlight feature & re-draw map
                if qlayer:
                    qlayer.SetCmd(self.AddTmpVectorMapLayer(mapName, cats,
                                                            useId = False,
                                                            addLayer = False))
                else:
                    qlayer = self.AddTmpVectorMapLayer(mapName, cats, useId = False)
                
                # set opacity based on queried layer
                opacity = layer.maplayer.GetOpacity(float = True)
                qlayer.SetOpacity(opacity)
                
                self.MapWindow.UpdateMap(render = False, renderVector = False)
            if not self.dialogs['attributes'].IsShown():
                self.dialogs['attributes'].Show()
        else:
            if qlayer:
                self.Map.DeleteLayer(qlayer)
                self.MapWindow.UpdateMap(render = False, renderVector = False)
            if self.dialogs['attributes'].IsShown():
                self.dialogs['attributes'].Hide()
        
    def OnQuery(self, event):
        """!Query tools menu"""
        if self.GetMapToolbar():
            self.SwitchTool(self.toolbars['map'], event)
        
        self.toolbars['map'].action['desc'] = 'queryMap'
        self.MapWindow.mouse['use'] = "query"

        if not self.IsStandalone():
            # switch to output console to show query results
            self._layerManager.notebook.SetSelectionByName('output')
        
        self.MapWindow.mouse['box'] = "point"
        self.MapWindow.zoomtype = 0
        
        # change the cursor
        self.MapWindow.SetCursor(self.cursors["cross"])
        
    def AddTmpVectorMapLayer(self, name, cats, useId = False, addLayer = True):
        """!Add temporal vector map layer to map composition

        @param name name of map layer
        @param useId use feature id instead of category 
        """
        # color settings from ATM
        color = UserSettings.Get(group = 'atm', key = 'highlight', subkey = 'color')
        colorStr = str(color[0]) + ":" + \
            str(color[1]) + ":" + \
            str(color[2])

        # icon used in vector display and its size
        icon = ''
        size = 0
        vparam = self.tree.GetLayerInfo(self.tree.layer_selected, key = 'cmd')
        for p in vparam:
            if '=' in p:
                parg,pval = p.split('=', 1)
                if parg == 'icon': icon = pval
                elif parg == 'size': size = float(pval)

        pattern = ["d.vect",
                   "map=%s" % name,
                   "color=%s" % colorStr,
                   "fcolor=%s" % colorStr,
                   "width=%d"  % UserSettings.Get(group = 'atm', key = 'highlight', subkey = 'width')]
        if icon != '':
            pattern.append('icon=%s' % icon)
        if size > 0:
            pattern.append('size=%i' % size)
        
        if useId:
            cmd = pattern
            cmd.append('-i')
            cmd.append('cats=%s' % str(cats))
        else:
            cmd = []
            for layer in cats.keys():
                cmd.append(copy.copy(pattern))
                lcats = cats[layer]
                cmd[-1].append("layer=%d" % layer)
                cmd[-1].append("cats=%s" % ListOfCatsToRange(lcats))
        
        if addLayer:
            if useId:
                return self.Map.AddLayer(ltype = 'vector', name = globalvar.QUERYLAYER, command = cmd,
                                         active = True, hidden = True, opacity = 1.0)
            else:
                return self.Map.AddLayer(ltype = 'command', name = globalvar.QUERYLAYER, command = cmd,
                                         active = True, hidden = True, opacity = 1.0)
        else:
            return cmd

    def OnMeasure(self, event):
        """!Init measurement routine that calculates map distance
        along transect drawn on map display
        """
        self.totaldist = 0.0 # total measured distance
        
        self.SwitchTool(self.toolbars['map'], event)

        # change mouse to draw line for measurement
        self.MapWindow.mouse['use'] = "measure"
        self.MapWindow.mouse['box'] = "line"
        self.MapWindow.zoomtype = 0
        self.MapWindow.pen     = wx.Pen(colour = 'red', width = 2, style = wx.SHORT_DASH)
        self.MapWindow.polypen = wx.Pen(colour = 'green', width = 2, style = wx.SHORT_DASH)
        
        # change the cursor
        self.MapWindow.SetCursor(self.cursors["pencil"])
        
        # initiating output (and write a message)
        # e.g., in Layer Manager switch to output console
        # TODO: this should be something like: write important message or write tip
        # TODO: mixed 'switching' and message? no, measuring handles 'swithing' on its own
        self._giface.WriteWarning(_('Click and drag with left mouse button '
                                              'to measure.%s'
                                              'Double click with left button to clear.') % \
                                                (os.linesep))
        if self.Map.projinfo['proj'] != 'xy':
            units = self.Map.projinfo['units']
            self._giface.WriteCmdLog(_('Measuring distance') + ' ('
                                      + units + '):')
        else:
            self._giface.WriteCmdLog(_('Measuring distance:'))
        
        if self.Map.projinfo['proj'] == 'll':
            try:
                import grass.lib.gis as gislib
                global haveCtypes
                haveCtypes = True

                gislib.G_begin_distance_calculations()
            except ImportError, e:
                self._giface.WriteWarning(_('Geodesic distance is not yet '
                                                          'supported by this tool.\n'
                                                          'Reason: %s' % e))
        
    def MeasureDist(self, beginpt, endpt):
        """!Calculate map distance from screen distance
        and print to output window
        """
        
        dist, (north, east) = self.MapWindow.Distance(beginpt, endpt)
        
        dist = round(dist, 3)
        d, dunits = self.FormatDist(dist)
        
        self.totaldist += dist
        td, tdunits = self.FormatDist(self.totaldist)
        
        strdist = str(d)
        strtotdist = str(td)
        
        if self.Map.projinfo['proj'] == 'xy' or 'degree' not in self.Map.projinfo['unit']:
            angle = int(math.degrees(math.atan2(north,east)) + 0.5)
            angle = 180 - angle
            if angle < 0:
                angle = 360 + angle
            
            mstring = '%s = %s %s\n%s = %s %s\n%s = %d %s\n%s' \
                % (_('segment'), strdist, dunits,
                   _('total distance'), strtotdist, tdunits,
                   _('bearing'), angle, _('deg'),
                   '-' * 60)
        else:
            mstring = '%s = %s %s\n%s = %s %s\n%s' \
                % (_('segment'), strdist, dunits,
                   _('total distance'), strtotdist, tdunits,
                   '-' * 60)
        
        self._giface.WriteLog(mstring, priority=2)
        
        return dist

    def OnProfile(self, event):
        """!Launch profile tool
        """
        raster = []
        if self.tree.layer_selected and \
                self.tree.GetLayerInfo(self.tree.layer_selected, key = 'type') == 'raster':
            raster.append(self.tree.GetLayerInfo(self.tree.layer_selected, key = 'maplayer').name)

        win = ProfileFrame(parent = self, rasterList = raster)
        
        win.CentreOnParent()
        win.Show()
        # Open raster select dialog to make sure that a raster (and
        # the desired raster) is selected to be profiled
        win.OnSelectRaster(None)

    def FormatDist(self, dist):
        """!Format length numbers and units in a nice way,
        as a function of length. From code by Hamish Bowman
        Grass Development Team 2006"""
        
        mapunits = self.Map.projinfo['units']
        if mapunits == 'metres':
            mapunits = 'meters'
        outunits = mapunits
        dist = float(dist)
        divisor = 1.0
        
        # figure out which units to use
        if mapunits == 'meters':
            if dist > 2500.0:
                outunits = 'km'
                divisor = 1000.0
            else: outunits = 'm'
        elif mapunits == 'feet':
            # nano-bug: we match any "feet", but US Survey feet is really
            #  5279.9894 per statute mile, or 10.6' per 1000 miles. As >1000
            #  miles the tick markers are rounded to the nearest 10th of a
            #  mile (528'), the difference in foot flavours is ignored.
            if dist > 5280.0:
                outunits = 'miles'
                divisor = 5280.0
            else:
                outunits = 'ft'
        elif 'degree' in mapunits and \
                not haveCtypes:
            if dist < 1:
                outunits = 'min'
                divisor = (1/60.0)
            else:
                outunits = 'deg'
        else:
            outunits = 'meters'
        
        # format numbers in a nice way
        if (dist/divisor) >= 2500.0:
            outdist = round(dist/divisor)
        elif (dist/divisor) >= 1000.0:
            outdist = round(dist/divisor,1)
        elif (dist/divisor) > 0.0:
            outdist = round(dist/divisor,int(math.ceil(3-math.log10(dist/divisor))))
        else:
            outdist = float(dist/divisor)
        
        return (outdist, outunits)
    

    def OnHistogramPyPlot(self, event):
        """!Init PyPlot histogram display canvas and tools
        """
        raster = []

        for layer in self.tree.GetSelections():
            if self.tree.GetLayerInfo(layer, key = 'maplayer').GetType() != 'raster':
                continue
            raster.append(self.tree.GetLayerInfo(layer, key = 'maplayer').GetName())

        win = Histogram2Frame(parent = self, rasterList = raster)
        win.CentreOnParent()
        win.Show()
        # Open raster select dialog to make sure that a raster (and the desired raster)
        # is selected to be histogrammed
        win.OnSelectRaster(None)
        
    def OnScatterplot(self, event):
        """!Init PyPlot scatterplot display canvas and tools
        """
        raster = []

        for layer in self.tree.GetSelections():
            if self.tree.GetLayerInfo(layer, key = 'maplayer').GetType() != 'raster':
                continue
            raster.append(self.tree.GetLayerInfo(layer, key = 'maplayer').GetName())
            
        win = ScatterFrame(parent = self, rasterList = raster)
        
        win.CentreOnParent()
        win.Show()
        # Open raster select dialog to make sure that at least 2 rasters (and the desired rasters)
        # are selected to be plotted
        win.OnSelectRaster(None)

    def OnHistogram(self, event):
        """!Init histogram display canvas and tools
        """
        win = HistogramFrame(self)
        
        win.CentreOnParent()
        win.Show()
        win.Refresh()
        win.Update()
       
    def OnAddBarscale(self, event):
        """!Handler for scale/arrow map decoration menu selection.
        """
        if self.dialogs['barscale']:
            return
        self.SwitchTool(self.toolbars['map'], event)
        
        id = 0 # unique index for overlay layer

        # If location is latlon, only display north arrow (scale won't work)
        #        proj = self.Map.projinfo['proj']
        #        if proj == 'll':
        #            barcmd = 'd.barscale -n'
        #        else:
        #            barcmd = 'd.barscale'

        # decoration overlay control dialog
        self.dialogs['barscale'] = \
            DecorationDialog(parent = self, title = _('Scale and North arrow'),
                             size = (350, 200),
                             style = wx.DEFAULT_DIALOG_STYLE | wx.CENTRE,
                             cmd = ['d.barscale', 'at=0,95'],
                             ovlId = id,
                             name = 'barscale',
                             checktxt = _("Show/hide scale and North arrow"),
                             ctrltxt = _("scale object"))

        self.dialogs['barscale'].CentreOnParent()
        ### dialog cannot be show as modal - in the result d.barscale is not selectable
        ### self.dialogs['barscale'].ShowModal()
        self.dialogs['barscale'].Show()
        self.MapWindow.mouse['use'] = 'pointer'

    def OnAddLegend(self, event):
        """!Handler for legend map decoration menu selection.
        """
        if self.dialogs['legend']:
            return
        
        id = 1 # index for overlay layer in render
        
        cmd = ['d.legend', 'at=5,50,2,5']
        
        if self.tree and self.tree.layer_selected and \
                self.tree.GetLayerInfo(self.tree.layer_selected, key = 'type') == 'raster':
            cmd.append('map=%s' % self.tree.GetLayerInfo(self.tree.layer_selected, key = 'maplayer').name)
        
        # Decoration overlay control dialog
        self.dialogs['legend'] = \
            DecorationDialog(parent = self, title = ('Legend'),
                             size = (350, 200),
                             style = wx.DEFAULT_DIALOG_STYLE | wx.CENTRE,
                             cmd = cmd,
                             ovlId = id,
                             name = 'legend',
                             checktxt = _("Show/hide legend"),
                             ctrltxt = _("legend object")) 
            
        self.dialogs['legend'].CentreOnParent() 
        ### dialog cannot be show as modal - in the result d.legend is not selectable
        ### self.dialogs['legend'].ShowModal()
        self.dialogs['legend'].Show()
        self.MapWindow.mouse['use'] = 'pointer'
        
    def OnAddText(self, event):
        """!Handler for text decoration menu selection.
        """
        self.SwitchTool(self.toolbars['map'], event)
        if self.MapWindow.dragid > -1:
            id = self.MapWindow.dragid
            self.MapWindow.dragid = -1
        else:
            # index for overlay layer in render
            if len(self.MapWindow.textdict.keys()) > 0:
                id = max(self.MapWindow.textdict.keys()) + 1
            else:
                id = 101
        
        self.dialogs['text'] = TextLayerDialog(parent = self, ovlId = id, 
                                               title = _('Add text layer'),
                                               size = (400, 200))
        self.dialogs['text'].CenterOnParent()

        # If OK button pressed in decoration control dialog
        if self.dialogs['text'].ShowModal() == wx.ID_OK:
            text = self.dialogs['text'].GetValues()['text']
            active = self.dialogs['text'].GetValues()['active']
        
            # delete object if it has no text or is not active
            if text == '' or active == False:
                try:
                    self.MapWindow2D.pdc.ClearId(id)
                    self.MapWindow2D.pdc.RemoveId(id)
                    del self.MapWindow.textdict[id]
                    if self.IsPaneShown('3d'):
                        self.MapWindow3D.UpdateOverlays()
                        self.MapWindow.UpdateMap()
                    else:
                        self.MapWindow2D.UpdateMap(render = False, renderVector = False)
                except:
                    pass
                return

            
            self.MapWindow.textdict[id] = self.dialogs['text'].GetValues()
            
            if self.IsPaneShown('3d'):
                self.MapWindow3D.UpdateOverlays()
                self.MapWindow3D.UpdateMap()
            else:
                self.MapWindow2D.pdc.ClearId(id)
                self.MapWindow2D.pdc.SetId(id)
                self.MapWindow2D.UpdateMap(render = False, renderVector = False)
            
        self.MapWindow.mouse['use'] = 'pointer'
    
    def OnAddArrow(self, event):
        """!Handler for north arrow menu selection.
            Opens Appearance page of nviz notebook.
        """
        
        self._layerManager.nviz.SetPage('decoration')
        self.MapWindow3D.SetDrawArrow((70, 70))
        
    def GetOptData(self, dcmd, type, params, propwin):
        """!Callback method for decoration overlay command generated by
        dialog created in menuform.py
        """
        # Reset comand and rendering options in render.Map. Always render decoration.
        # Showing/hiding handled by PseudoDC
        self.Map.ChangeOverlay(ovltype = type, type = 'overlay', name = '', command = dcmd,
                               active = True, render = False)
        self.params[type] = params
        self.propwin[type] = propwin

    def OnZoomToMap(self, event):
        """!Set display extents to match selected raster (including
        NULLs) or vector map.
        """
        layers = None
        if self.IsStandalone():
            layers = self.MapWindow.GetMap().GetListOfLayers(active = False)
        
        self.MapWindow.ZoomToMap(layers = layers)

    def OnZoomToRaster(self, event):
        """!Set display extents to match selected raster map (ignore NULLs)
        """
        self.MapWindow.ZoomToMap(ignoreNulls = True)
        
    def OnZoomToSaved(self, event):
        """!Set display geometry to match extents in
        saved region file
        """
        self.MapWindow.ZoomToSaved()
        
    def OnDisplayToWind(self, event):
        """!Set computational region (WIND file) to match display
        extents
        """
        self.MapWindow.DisplayToWind()
 
    def OnSaveDisplayRegion(self, event):
        """!Save display extents to named region file.
        """
        self.MapWindow.SaveRegion(display = True)

    def OnSaveWindRegion(self, event):
        """!Save computational region to named region file.
        """
        self.MapWindow.SaveRegion(display = False)
        
    def OnZoomMenu(self, event):
        """!Popup Zoom menu
        """
        zoommenu = wx.Menu()
        
        for label, handler in ((_('Zoom to computational region'), self.OnZoomToWind),
                               (_('Zoom to default region'), self.OnZoomToDefault),
                               (_('Zoom to saved region'), self.OnZoomToSaved),
                               (_('Set computational region from display extent'), self.OnDisplayToWind),
                               (_('Save display geometry to named region'), self.OnSaveDisplayRegion),
                               (_('Save computational region to named region'), self.OnSaveWindRegion)):
            mid = wx.MenuItem(zoommenu, wx.ID_ANY, label)
            zoommenu.AppendItem(mid)
            self.Bind(wx.EVT_MENU, handler, mid)
            
        # Popup the menu. If an item is selected then its handler will
        # be called before PopupMenu returns.
        self.PopupMenu(zoommenu)
        zoommenu.Destroy()

    def SetProperties(self, render = False, mode = 0, showCompExtent = False,
                      constrainRes = False, projection = False, alignExtent = True):
        """!Set properies of map display window"""
        self.SetProperty('render', render)
        self.statusbarManager.SetMode(mode)
        self.StatusbarUpdate()
        self.SetProperty('region', showCompExtent)
        self.SetProperty('alignExtent', alignExtent)
        self.SetProperty('resolution', constrainRes)
        self.SetProperty('projection', projection)
        
    def IsStandalone(self):
        """!Check if Map display is standalone"""
        if self._layerManager:
            return False
        
        return True
    
    def GetLayerManager(self):
        """!Get reference to Layer Manager

        @return window reference
        @return None (if standalone)
        """
        return self._layerManager
    
    def GetMapToolbar(self):
        """!Returns toolbar with zooming tools"""
        return self.toolbars['map']

    def OnVNet(self, event):
        """!Dialog for v.net* modules 
        """
        if self.dialogs['vnet']:
            self.dialogs['vnet'].Raise()
            return
        
        from vnet.dialogs import VNETDialog
        self.dialogs['vnet'] = VNETDialog(parent = self)
        self.dialogs['vnet'].CenterOnScreen()
        self.dialogs['vnet'].Show()
            
    def SwitchTool(self, toolbar, event):
        """!Calls UpdateTools to manage connected toolbars"""
        self.UpdateTools(event)
        SingleMapFrame.SwitchTool(self, toolbar, event)

    def UpdateTools(self, event):
        """!Method deals with relations of toolbars and other
        elements""" 
        # untoggles button in other toolbars
        for toolbar in self.toolbars.itervalues():
            if hasattr(event, 'GetEventObject') == True:
                if event.GetEventObject() == toolbar:
                    continue
            toolbar.ToggleTool(toolbar.action['id'], False)
            toolbar.action['id'] = -1
            toolbar.OnTool(None)
        
        # mouse settings
        self.MapWindow.mouse['box'] = 'point' 
        self.MapWindow.mouse['use'] = 'pointer'
        
        # untoggles button in add legend dialog
        if self.dialogs['legend']:
            if hasattr(event ,'GetEventObject'):
                if event.GetEventObject().GetId() == \
                        self.dialogs['legend'].FindWindowByName('resize').GetId():
                    return
            self.dialogs['legend'].FindWindowByName('resize').SetValue(0)
